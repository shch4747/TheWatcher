"""Proposals (ADR-0007: waits are rows; Spec: Chat Agent): a change the bot
wants to make but won't without a 👍. Owned by the WhatsApp Agent - the
Gateway delivers the reactions and persists the rows.

Internal to `agents/wa_agent`; `interface.py` re-exports the public names.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from shared.cms.interface import CmsClientProtocol, MemberRecord, default_cms_client, fuzzy_match_member
from shared.config import settings
from shared.gateway.interface import (
    ProposalStore,
    get_channel_by_kind,
    is_bot_admin,
    link_member,
    resolve_sender,
    send,
)
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

THUMBS_UP = {"\U0001f44d", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿"}

Executor = Callable[[dict, str, VaultClient], Awaitable[str]]


async def execute_link_member(data: dict, confirmed_by: str, vault: VaultClient) -> str:
    await link_member(data["wa_identity"], data["member_title"], data.get("cms_member_id"), confirmed_by)
    return f"Linked to [[{data['member_title']}]]."


async def execute_create_member(data: dict, confirmed_by: str, vault: VaultClient) -> str:
    title = data["display_name"]
    try:
        await vault.create(member_page_path(title), render_new_member_page(title))
    except PageExists:
        pass
    await link_member(data["wa_identity"], title, None, confirmed_by)
    return f"Created [[{title}]] and linked."


async def execute_wiki_write(data: dict, confirmed_by: str, vault: VaultClient) -> str:
    path, section, line = data["path"], data["section"], data["line"]
    current = await vault.read(path)
    page = append_lines(parse_page(current.content), section, [line], allow_human=True)
    await vault.write(path, dump_page(page), base_revision=current.revision)
    return f"Added to {path} ({section})."


_EXECUTORS: dict[str, Executor] = {
    "link_member": execute_link_member,
    "create_member": execute_create_member,
    "wiki_write": execute_wiki_write,
}


class ProposalService:
    def __init__(self, store: ProposalStore | None = None, vault: VaultClient | None = None) -> None:
        self.store = store or ProposalStore()
        self.vault = vault

    async def propose(self, channel: str | None, kind: str, payload: dict, text: str) -> None:
        expires_at = datetime.now(UTC) + timedelta(hours=settings.proposal_expiry_hours)
        proposal_id = await self.store.create(channel or "unknown", kind, payload, expires_at)
        if channel is None:
            return
        result = await send(channel, text)
        await self.store.set_message_id(proposal_id, result.message_id)

    async def propose_wiki_write(
        self, channel_jid: str, path: str, section: str, line: str, preview_note: str | None = None
    ) -> str:
        text = preview_note or f"I'll add this to {path} ({section}):\n> {line}\n\n\U0001f44d to confirm."
        await self.propose(channel_jid, "wiki_write", {"path": path, "section": section, "line": line}, text)
        return text

    async def propose_identity_link(
        self,
        wa_identity: str,
        display_name: str,
        cms_client: CmsClientProtocol | None = None,
    ) -> str:
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

        await self.propose(coordis.jid if coordis else None, kind, payload, text)
        return text

    async def confirm(self, proposal_id: int, confirmed_by: str, vault: VaultClient | None = None) -> str:
        proposal = await self.store.get(proposal_id)
        if proposal is None:
            return "No such proposal."
        if proposal.status != "pending":
            return f"Proposal already {proposal.status}."
        if proposal.expires_at and datetime.now(UTC) > proposal.expires_at:
            await self.store.mark_expired(proposal_id)
            return "That proposal expired."

        executor = _EXECUTORS.get(proposal.kind)
        if executor is None:
            return f"Unknown proposal kind: {proposal.kind}"
        reply = await executor(proposal.payload, confirmed_by, vault or self.vault or default_vault_client())
        await self.store.mark_confirmed(proposal_id, confirmed_by, datetime.now(UTC))
        return reply

    async def handle_reaction(self, reactor: str, message_id: str | None, emoji: str | None) -> str | None:
        if not message_id or emoji not in THUMBS_UP:
            return None
        if not await is_bot_admin(reactor):
            return None
        proposal = await self.store.get_pending_by_message(message_id)
        if proposal is None:
            return None
        return await self.confirm(proposal.id, confirmed_by=reactor)

    async def expire_stale(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        expired = await self.store.expire_due(now)
        for record in expired:
            if record.channel and record.channel != "unknown":
                await send(record.channel, f"Proposal #{record.id} expired without a \U0001f44d.")
        return len(expired)

    async def pending_count(self) -> int:
        return await self.store.pending_count()


_default = ProposalService()


async def propose_wiki_write(
    channel_jid: str, path: str, section: str, line: str, preview_note: str | None = None
) -> str:
    return await _default.propose_wiki_write(channel_jid, path, section, line, preview_note)


async def propose_identity_link(
    wa_identity: str,
    display_name: str,
    cms_client: CmsClientProtocol | None = None,
) -> str:
    return await _default.propose_identity_link(wa_identity, display_name, cms_client)


async def confirm_proposal(proposal_id: int, confirmed_by: str, vault: VaultClient | None = None) -> str:
    return await _default.confirm(proposal_id, confirmed_by, vault)


async def handle_reaction(reactor: str, message_id: str | None, emoji: str | None) -> str | None:
    return await _default.handle_reaction(reactor, message_id, emoji)


async def expire_stale_proposals(now: datetime | None = None) -> int:
    return await _default.expire_stale(now)


async def pending_count() -> int:
    return await _default.pending_count()
