"""Proposals (ADR-0007: waits are rows; Spec: Chat Agent): a change the bot
wants to make but won't without a 👍. Owned by the WhatsApp Agent - the
Gateway only delivers the reactions (a reaction hook `main.py` wires to
`handle_reaction`) and keeps the Member Registry that two kinds of
Proposal write to.

The whole lifecycle is here: `propose_*` posts the exact change and
stores it pending with an expiry; a Bot Admin's 👍 on that message runs
`confirm_proposal`, which executes it through the executor for its kind
and marks it confirmed; `expire_stale_proposals` drops what nobody
confirmed in time, with a one-line notice. Unknown kinds are refused,
never silently ignored.

Kinds:
- `wiki_write` - append a line to a section of a page (a Chat Agent
  "note that X"). Goes through the page editor like any write; a human
  section is allowed because a human confirmed it.
- `link_member` - link a WhatsApp identity to an existing member.
- `create_member` - create a member page and link the identity to it.

Internal to `agents/wa_agent`; `interface.py` re-exports the public names.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from shared.cms.interface import CmsClientProtocol, MemberRecord, default_cms_client, fuzzy_match_member
from shared.config import settings
from shared.db import Proposal, aware_utc, get_session
from shared.gateway.interface import get_channel_by_kind, is_bot_admin, link_member, resolve_sender, send
from shared.wiki.interface import (
    PageExists,
    VaultClient,
    append_lines,
    default_vault_client,
    dump_page,
    member_page_path,
    parse_page,
    render_new_member_page,
)
from sqlalchemy import select

THUMBS_UP = {"\U0001f44d", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿"}

Executor = Callable[[dict, str, VaultClient], Awaitable[str]]


# --------------------------------------------------------------------------
# executors
# --------------------------------------------------------------------------


async def _link_member(data: dict, confirmed_by: str, vault: VaultClient) -> str:
    await link_member(data["wa_identity"], data["member_title"], data.get("cms_member_id"), confirmed_by)
    return f"Linked to [[{data['member_title']}]]."


async def _create_member(data: dict, confirmed_by: str, vault: VaultClient) -> str:
    title = data["display_name"]
    try:
        await vault.create(member_page_path(title), render_new_member_page(title))
    except PageExists:
        pass  # a page with that name already exists - link to it, never replace it
    await link_member(data["wa_identity"], title, None, confirmed_by)
    return f"Created [[{title}]] and linked."


async def _wiki_write(data: dict, confirmed_by: str, vault: VaultClient) -> str:
    path, section, line = data["path"], data["section"], data["line"]
    current = await vault.read(path)
    page = append_lines(parse_page(current.content), section, [line], allow_human=True)
    await vault.write(path, dump_page(page), base_revision=current.revision)
    return f"Added to {path} ({section})."


_EXECUTORS: dict[str, Executor] = {
    "link_member": _link_member,
    "create_member": _create_member,
    "wiki_write": _wiki_write,
}


# --------------------------------------------------------------------------
# proposing
# --------------------------------------------------------------------------


async def _propose(channel: str | None, kind: str, payload: dict, text: str) -> None:
    """Store the Proposal pending, post it, and remember which message
    carries it (that's what a 👍 reacts to)."""
    expires_at = datetime.now(UTC) + timedelta(hours=settings.proposal_expiry_hours)
    async with get_session() as session:
        proposal = Proposal(
            channel=channel or "unknown", kind=kind, payload=json.dumps(payload), expires_at=expires_at
        )
        session.add(proposal)
        await session.commit()
        proposal_id = proposal.id
    if channel is None:
        return
    result = await send(channel, text)
    async with get_session() as session:
        row = await session.get(Proposal, proposal_id)
        if row is not None:
            row.message_id = result.message_id
            await session.commit()


async def propose_wiki_write(
    channel_jid: str, path: str, section: str, line: str, preview_note: str | None = None
) -> str:
    """Any Chat Agent write is a Proposal (Spec: Chat Agent) - the bot
    posts the exact change and waits for a thumbs-up. `line` is the
    literal text appended to `section` in `path` on confirm."""
    text = preview_note or f"I'll add this to {path} ({section}):\n> {line}\n\n\U0001f44d to confirm."
    await _propose(channel_jid, "wiki_write", {"path": path, "section": section, "line": line}, text)
    return text


async def propose_identity_link(
    wa_identity: str,
    display_name: str,
    cms_client: CmsClientProtocol | None = None,
) -> str:
    """Unknown sender -> fuzzy match against CMS member titles -> Proposal
    to Bot Admins in coordis. No match proposes creating a member page
    instead of guessing (Spec: Gateway identity)."""
    already = await resolve_sender(wa_identity)
    if already is not None:
        return f"{wa_identity} is already linked to [[{already.member_title}]]."

    cms_client = cms_client or default_cms_client()
    candidates: list[MemberRecord] = await cms_client.list_members()
    match = fuzzy_match_member(display_name, candidates)
    coordis = await get_channel_by_kind("coordis")

    if match is not None:
        payload = {"wa_identity": wa_identity, "member_title": match.title, "cms_member_id": match.cms_id}
        text = f"Link *{display_name}* to [[{match.title}]]? \U0001f44d"
        kind = "link_member"
    else:
        payload = {"wa_identity": wa_identity, "display_name": display_name}
        text = f"No member found for *{display_name}* - create a member page and link it? \U0001f44d"
        kind = "create_member"

    await _propose(coordis.jid if coordis else None, kind, payload, text)
    return text


# --------------------------------------------------------------------------
# resolving
# --------------------------------------------------------------------------


async def confirm_proposal(proposal_id: int, confirmed_by: str, vault: VaultClient | None = None) -> str:
    """Execute a pending Proposal (Spec: 👍 within 24h executes it). The
    executor runs outside any open DB session - it writes the registry
    and the wiki itself - and the Proposal is marked confirmed after it
    succeeds, so a failed execution leaves it pending."""
    async with get_session() as session:
        proposal = await session.get(Proposal, proposal_id)
        if proposal is None:
            return "No such proposal."
        if proposal.status != "pending":
            return f"Proposal already {proposal.status}."
        expires_at = aware_utc(proposal.expires_at)
        if expires_at and datetime.now(UTC) > expires_at:
            proposal.status = "expired"
            await session.commit()
            return "That proposal expired."
        kind, payload = proposal.kind, json.loads(proposal.payload)

    executor = _EXECUTORS.get(kind)
    if executor is None:
        return f"Unknown proposal kind: {kind}"
    reply = await executor(payload, confirmed_by, vault or default_vault_client())

    async with get_session() as session:
        row = await session.get(Proposal, proposal_id)
        if row is not None:
            row.status = "confirmed"
            row.resolved_at = datetime.now(UTC)
            row.resolved_by = confirmed_by
            await session.commit()
    return reply


async def handle_reaction(reactor: str, message_id: str | None, emoji: str | None) -> str | None:
    """A 👍 from a Bot Admin executes the reacted-to Proposal. Only Bot
    Admins confirm for now - distinguishing an Initiative lead needs the
    page's `lead:`, which isn't wired in yet."""
    if not message_id or emoji not in THUMBS_UP:
        return None
    if not await is_bot_admin(reactor):
        return None
    async with get_session() as session:
        proposal = await session.scalar(
            select(Proposal).where(Proposal.message_id == message_id, Proposal.status == "pending")
        )
    if proposal is None:
        return None
    return await confirm_proposal(proposal.id, confirmed_by=reactor)


async def expire_stale_proposals(now: datetime | None = None) -> int:
    """Scheduler job body: expired proposals are dropped with a one-line
    notice, never silently forgotten."""
    now = now or datetime.now(UTC)
    async with get_session() as session:
        pending = await session.scalars(select(Proposal).where(Proposal.status == "pending"))
        expired = [p for p in pending if p.expires_at and aware_utc(p.expires_at) < now]
        for p in expired:
            p.status = "expired"
        await session.commit()
        channels_and_ids = [(p.channel, p.id) for p in expired]

    for channel, proposal_id in channels_and_ids:
        if channel and channel != "unknown":
            await send(channel, f"Proposal #{proposal_id} expired without a \U0001f44d.")
    return len(channels_and_ids)


async def pending_count() -> int:
    async with get_session() as session:
        return len(list(await session.scalars(select(Proposal).where(Proposal.status == "pending"))))
