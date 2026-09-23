"""Gateway interface (ADR-0003): the only entry/exit point for WhatsApp.
Every other package calls these functions, never `gowa_client` or the DB
tables directly. Phase 0: webhook intake + send. Phase 2 (this module,
extended): edge filtering (allowlist, DMs, admin commands before
allowlisting), commands, singleton enforcement, channel/initiative page
creation, /link.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone

from pydantic import BaseModel
from sqlalchemy import select

from shared.cms.interface import CmsClientProtocol, MemberRecord, default_cms_client, fuzzy_match_member
from shared.config import settings
from shared.db import (
    BotAdmin,
    Channel,
    MembersRegistry,
    MessageBuffer,
    OutboundLog,
    Proposal,
    SetupSession,
    get_session,
)

# The message buffer's interface, re-exported: consumers read messages
# through these, never through `messages_buffer` rows.
from shared.gateway.buffer import InboundMessage as InboundMessage
from shared.gateway.buffer import get_context as get_context
from shared.gateway.buffer import get_message as get_message
from shared.gateway.buffer import get_messages as get_messages
from shared.gateway.buffer import mark_consumed as mark_consumed
from shared.gateway.buffer import pending_messages as pending_messages
from shared.gateway.buffer import to_inbound
from shared.gateway.commands import (
    ChannelsCommand,
    Command,
    HealthCommand,
    IngestCommand,
    LinkCommand,
    SetupCommand,
    StatusCommand,
    UnwatchCommand,
    is_group_jid,
    parse_command,
)
from shared.gateway.events import parse_gowa_event, wrap_backfilled_message
from shared.gateway.gowa_client import GowaClient
from shared.scheduler.interface import due_jobs, ledger_tail, run_job
from shared.wiki.interface import (
    LapisClient,
    PageExists,
    VaultClient,
    append_lines,
    check_vault_connection,
    default_vault_client,
    dump_page,
    parse_page,
    read_if_exists,
    render_new_channel_page,
    render_new_event_page,
    render_new_member_page,
    render_new_project_page,
    slugify,
)

logger = logging.getLogger(__name__)

_client = GowaClient()

SETUP_STEPS = ["lead", "brief", "timeline"]

# Real-time message hooks (e.g. the Chat Agent) are registered here
# rather than imported directly - shared/gateway must not depend on
# agents/wa_agent (AGENTS.md: agents depend on shared, never the
# reverse). The application entrypoint wires this at startup.
MessageHook = Callable[[InboundMessage], Awaitable[None]]
_message_hooks: list[MessageHook] = []


def register_message_hook(hook: MessageHook) -> None:
    """Called for every non-command, non-reaction message that lands in
    a group already allowlisted (or being set up). A hook that raises is
    logged and swallowed - a broken Chat Agent must never break webhook
    intake."""
    _message_hooks.append(hook)


class SendResult(BaseModel):
    message_id: str | None


def verify_signature(body: bytes, signature: str | None) -> bool:
    """HMAC-SHA256 over the raw body, checked against the current secret
    and, during a rotation window, the previous one too - so updating
    gowa's webhook secret doesn't have to happen in the same instant as
    updating ours (security pass: webhook secret rotation).

    gowa sends the header as `sha256={hex}`, not a bare hex digest (see
    docs/webhook-payload.md in the gowa repo) - the prefix must be
    stripped before comparing.
    """
    if not signature:
        return False
    received = signature.removeprefix("sha256=")
    secrets = [settings.gowa_webhook_secret]
    if settings.gowa_webhook_secret_previous:
        secrets.append(settings.gowa_webhook_secret_previous)
    return any(
        hmac.compare_digest(hmac.new(s.encode(), body, hashlib.sha256).hexdigest(), received)
        for s in secrets
        if s
    )


async def is_bot_admin(wa_identity: str) -> bool:
    async with get_session() as session:
        row = await session.get(BotAdmin, wa_identity)
    return row is not None


async def get_channel(jid: str) -> Channel | None:
    async with get_session() as session:
        return await session.scalar(select(Channel).where(Channel.jid == jid))


async def _is_allowlisted(jid: str) -> bool:
    channel = await get_channel(jid)
    return channel is not None and channel.kind != "unset"


async def receive_webhook(raw_body: bytes, signature: str | None) -> bool:
    """Verify, edge-filter (allowlist / DMs / admin commands pre-allowlist),
    dedupe by message id, and buffer a raw gowa event. Returns False (and
    buffers nothing) for a bad signature, a filtered-out chat, or a duplicate.
    Edits/revokes of an unprocessed message update the buffered row in
    place; of a processed one they're ignored (Spec: Gateway).
    """
    if not verify_signature(raw_body, signature):
        return False

    payload = json.loads(raw_body)
    event = parse_gowa_event(payload)
    message_id = event.message_id
    channel = event.chat_id or "unknown"
    event_type = event.event_type
    sender = event.sender
    text = event.text
    if not message_id:
        return False

    is_admin_command = bool(
        sender and text and text.strip().startswith("/") and await is_bot_admin(sender)
    )
    if not is_group_jid(channel):
        return False  # DMs are always ignored at the edge, even from admins
    if not is_admin_command and not await _is_allowlisted(channel):
        return False

    async with get_session() as session:
        existing = await session.scalar(select(MessageBuffer).where(MessageBuffer.message_id == message_id))
        if event_type in ("message.edited", "message.revoked") and existing is not None:
            if not existing.processed:
                existing.payload = raw_body.decode()
                existing.event_type = event_type
                # an edit is new activity: the batch's quiet period restarts
                existing.received_at = datetime.now(UTC)
                await session.commit()
                return True
            return False  # processed messages are immutable
        if existing is not None:
            return False
        buffered = MessageBuffer(
            message_id=message_id,
            channel=channel,
            event_type=event_type,
            payload=raw_body.decode(),
        )
        session.add(buffered)
        await session.commit()

    if is_admin_command:
        # commands are deterministic and act immediately - they don't wait
        # for a batch (Spec: Gateway commands). A failure here (most
        # commonly the reply send itself, e.g. gowa rejecting the
        # outbound call) must never crash the whole webhook request: the
        # command may have already taken effect (e.g. /setup wrote the
        # channel row) even if the confirmation couldn't be delivered,
        # and an unhandled exception here would both 500 the webhook
        # (gowa retries) and leave the message stuck unprocessed forever
        # (the dedupe check short-circuits every retry with nothing to
        # retry, since is_admin_command's branch never runs again for an
        # already-buffered message_id).
        assert sender is not None  # implied by is_admin_command being True
        try:
            reply = await handle_command(channel, sender, text)
            if reply:
                await send(channel, reply)
        except Exception:  # noqa: BLE001 - logged, never crashes webhook intake
            logger.exception("admin command handling failed for %s in %s", text, channel)
        async with get_session() as session:
            row = await session.get(MessageBuffer, buffered.id)
            if row is not None:
                row.processed = True
                await session.commit()
    elif event_type == "message.reaction" and sender:
        await handle_reaction(sender, event.reacted_message_id, event.reaction_emoji)
    elif event_type == "message":
        message = to_inbound(buffered)
        for hook in _message_hooks:
            try:
                await hook(message)
            except Exception:  # noqa: BLE001 - a broken hook must not break intake
                logger.exception("message hook %r failed for channel %s", hook, channel)
    return True


async def send(channel: str, text: str, reply_to: str | None = None) -> SendResult:
    """Send a message and log it. Never merges/batches sends (Spec: Gateway)."""
    result = await _client.send_text(channel, text, reply_message_id=reply_to)
    message_id = result.get("results", {}).get("message_id") or result.get("message_id")

    async with get_session() as session:
        session.add(OutboundLog(channel=channel, text=text, reply_to=reply_to, message_id=message_id))
        await session.commit()

    return SendResult(message_id=message_id)


async def _upsert_channel(jid: str, kind: str, title: str | None, initiative: str | None) -> None:
    async with get_session() as session:
        channel = await session.scalar(select(Channel).where(Channel.jid == jid))
        if channel is None:
            channel = Channel(jid=jid, kind=kind, title=title, initiative=initiative)
            session.add(channel)
        else:
            channel.kind = kind
            channel.title = title
            channel.initiative = initiative
        await session.commit()


async def _channel_by_kind(kind: str) -> Channel | None:
    async with get_session() as session:
        return await session.scalar(select(Channel).where(Channel.kind == kind))


async def check_gowa_connection() -> dict:
    """Health check: is gowa reachable and logged in? Separate from
    `/healthz` (which only says the watcher process itself is up) - a
    dead gowa session shouldn't look the same as a dead watcher."""
    return await _client.session_status()


async def get_channel_by_kind(kind: str) -> Channel | None:
    return await _channel_by_kind(kind)


async def list_channels(kind: str | None = None) -> list[Channel]:
    """Spec (Gateway interface, illustrative): `list_channels()`."""
    async with get_session() as session:
        stmt = select(Channel).where(Channel.kind != "unset")
        if kind:
            stmt = stmt.where(Channel.kind == kind)
        rows = await session.scalars(stmt)
    return list(rows)


async def _initiative_page_exists(vault: VaultClient, kind: str, slug: str) -> bool:
    path = f"{'projects' if kind == 'project' else 'events'}/{slug}.md"
    return await read_if_exists(vault, path) is not None


_DEFAULT_KIND_TITLES = {
    "coordis": "Coordis",
    "exes": "Exes",
    "research": "Research",
    "all": "All",
    "logs": "Logs",
    "other": "Other",
}


async def _ensure_channel_page(vault: VaultClient, title: str, kind: str) -> None:
    """`coordis`/`exes`/`research`/`all`/`logs`/`other` channels never
    got a `channels/<slug>.md` index page before - only project/event
    channels did - so their vault directory name was whatever
    `slugify(channel.title or channel_jid)` fell back to (the raw jid,
    since title was never set either), and there was nowhere for
    `regenerate_channel_threads_index` to write Active/Stale/Archived
    links even once titles were fixed. Idempotent: a repeat `/setup`
    (e.g. to backfill a title on an already-registered channel) never
    clobbers an existing page."""
    slug = slugify(title)
    path = f"channels/{slug}.md"
    try:
        await vault.create(path, render_new_channel_page(title, kind))
    except PageExists:
        pass


async def setup(channel_jid: str, requested_by: str, cmd: SetupCommand, vault: VaultClient) -> str:
    """`/setup [kind] [<Title>]`. Singleton kinds refuse a second channel;
    project/event with a missing initiative page opens a setup_session
    asking lead/brief/timeline instead of creating a half-formed page."""
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /setup."

    if cmd.kind in ("coordis", "exes", "research", "all", "logs", "other"):
        if cmd.kind != "other":
            existing = await _channel_by_kind(cmd.kind)
            if existing is not None and existing.jid != channel_jid:
                return f"A {cmd.kind} channel is already set up; refusing a second one."
        title = cmd.title or _DEFAULT_KIND_TITLES[cmd.kind]
        await _upsert_channel(channel_jid, cmd.kind, title, initiative=None)
        await _ensure_channel_page(vault, title, cmd.kind)
        return "watching"

    # project / event
    if not cmd.title:
        return f"/setup {cmd.kind} needs a title: /setup {cmd.kind} <Title>"

    slug = slugify(cmd.title)
    if not await _initiative_page_exists(vault, cmd.kind, slug):
        async with get_session() as session:
            session.add(
                SetupSession(channel=channel_jid, kind=cmd.kind, title=cmd.title, requested_by=requested_by)
            )
            await session.commit()
        return f'Setting up "{cmd.title}" as a new {cmd.kind}. Who is the lead? (reply with their name)'

    await _upsert_channel(channel_jid, cmd.kind, cmd.title, initiative=cmd.title)
    channel_content = render_new_channel_page(cmd.title, cmd.kind, initiative=cmd.title)
    try:
        await vault.create(f"channels/{slug}.md", channel_content)
    except PageExists:
        pass  # a repeat /setup never replaces the page (or its Notes)
    return "watching"


async def _active_setup_session(channel_jid: str) -> SetupSession | None:
    async with get_session() as session:
        return await session.scalar(
            select(SetupSession)
            .where(SetupSession.channel == channel_jid, SetupSession.step != "done")
            .order_by(SetupSession.created_at.desc())
        )


async def continue_setup_session(channel_jid: str, reply_text: str, vault: VaultClient) -> str | None:
    """Feed one chat reply into the in-progress setup_session for this
    channel, if any. Returns None if there's no session to continue."""
    session_row = await _active_setup_session(channel_jid)
    if session_row is None:
        return None

    async with get_session() as session:
        row = await session.get(SetupSession, session_row.id)
        assert row is not None
        setattr(row, row.step, reply_text.strip())
        idx = SETUP_STEPS.index(row.step)
        if idx + 1 < len(SETUP_STEPS):
            row.step = SETUP_STEPS[idx + 1]
            await session.commit()
            next_field = row.step
            return f"Got it. What's the {next_field}?"

        row.step = "done"
        lead, brief, timeline = row.lead, row.brief, row.timeline
        kind, title, requested_by = row.kind, row.title, row.requested_by
        await session.commit()

    slug = slugify(title)
    lead_name = lead or requested_by
    page = (
        render_new_project_page(title, lead=lead_name, brief=brief or "")
        if kind == "project"
        else render_new_event_page(title, lead=lead_name, brief=brief or "")
    )
    await vault.create(f"{'projects' if kind == 'project' else 'events'}/{slug}.md", page)

    await _upsert_channel(channel_jid, kind, title, initiative=title)
    channel_content = render_new_channel_page(title, kind, initiative=title)
    try:
        await vault.create(f"channels/{slug}.md", channel_content)
    except PageExists:
        pass
    return f'"{title}" created (timeline: {timeline}). watching'


async def unwatch(channel_jid: str, requested_by: str, target_jid: str | None = None) -> str:
    """`/unwatch` (no args) stops watching the channel it's sent from;
    `/unwatch <jid>` (jid from `/channels`) lets a Bot Admin remove a
    channel from any allowlisted-or-not group they can reach, without
    needing to be a member of the channel being removed."""
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /unwatch."
    target = target_jid or channel_jid
    async with get_session() as session:
        channel = await session.scalar(select(Channel).where(Channel.jid == target))
        if channel is None or channel.kind == "unset":
            return f"{target} wasn't being watched." if target_jid else "This channel wasn't being watched."
        channel.kind = "unset"
        channel.initiative = None
        await session.commit()
    return f"No longer watching {target}." if target_jid else "No longer watching this channel."


async def status(channel_jid: str) -> str:
    channel = await get_channel(channel_jid)
    if channel is None or channel.kind == "unset":
        return "Not watching this channel."
    return f"kind={channel.kind} initiative={channel.initiative or '-'} cursor={channel.cursor or '-'}"


async def _group_name_map() -> dict[str, str]:
    """jid -> WhatsApp group display name, best-effort (Spec: /channels
    needs a human-readable name, not just the jid it's keyed by
    internally). Empty on any gowa error - /channels still works, it
    just falls back to showing raw jids."""
    try:
        groups = await _client.list_groups()
        return {g["JID"]: g["Name"] for g in groups if g.get("JID") and g.get("Name")}
    except Exception:  # noqa: BLE001 - best-effort lookup, never breaks /channels
        return {}


async def channels_report(requested_by: str) -> str:
    """`/channels`: every currently-watched channel, with its WhatsApp
    group name (looked up live from gowa) and its jid - the jid is
    still shown alongside the name since `/unwatch <jid>` needs it.
    Bot Admin only, same as every other channel-management command."""
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /channels."
    channels = await list_channels()
    if not channels:
        return "No channels are being watched."
    names = await _group_name_map()
    lines = [f"*Watched channels* ({len(channels)})"]
    for channel in channels:
        label = f"{names[channel.jid]} ({channel.jid})" if channel.jid in names else channel.jid
        title = f" title={channel.title}" if channel.title else ""
        initiative = f" initiative={channel.initiative}" if channel.initiative else ""
        lines.append(f"- {label}  kind={channel.kind}{title}{initiative}")
    return "\n".join(lines)


async def trigger_ingest(requested_by: str) -> str:
    """`/ingest`: cut and process whatever's unprocessed right now,
    ignoring the BATCH_N/BATCH_T_MINUTES/BATCH_QUIET_MINUTES thresholds
    that gate the scheduled `ingest_tick` - useful right after `/setup`,
    or any time you don't want to wait out the quiet period while
    testing. Runs as the `ingest_now` job, which shares `ingest_tick`'s
    lock key so the two can never run concurrently."""
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /ingest."
    try:
        result = await run_job("ingest_now")
    except KeyError:
        return "ingest_now isn't registered - is the app running via main.py (not just uvicorn)?"
    if result.outcome == "success":
        return "Ingestion triggered ✅ (ignored batch thresholds - cut whatever was unprocessed)"
    return f"Ingestion failed: {result.error}"


async def health() -> str:
    """`/health`: connectivity + a few counts, for a Bot Admin to sanity
    check the deployment from their phone without shelling into the
    container. Checks are read-only and never make a real model call
    (that would cost money and add latency to a chat command) - the
    Decision/Worker/Mentor lines report whether they're *configured*,
    not whether the provider is currently reachable.
    """
    lines = ["*Watcher health*"]

    gowa_result = await check_gowa_connection()
    lines.append(f"gowa: {'✅ ok' if gowa_result.get('ok') else '❌ ' + str(gowa_result.get('error'))}")

    vault = default_vault_client()
    # unwrap the observability audit wrapper (ADR-0013) before asking
    # what backend this really is
    backend = getattr(vault, "inner", vault)
    vault_kind = "Lapis (live)" if isinstance(backend, LapisClient) else "local directory"
    vault_result = await check_vault_connection(vault)
    vault_status = "✅ ok" if vault_result.get("ok") else f"❌ {vault_result.get('error')}"
    lines.append(f"vault: {vault_status} ({vault_kind})")

    if settings.jev_base_url:
        decision_line = f"decision model: Jev ({settings.jev_base_url})"
    else:
        decision_line = "decision model: Worker-backed (no Jev configured)"
    lines.append(decision_line)
    lines.append(f"worker model: {settings.worker_model_name}")
    lines.append(f"mentor model: {settings.mentor_model_name}")

    async with get_session() as session:
        admin_count = len(list(await session.scalars(select(BotAdmin))))
        channel_count = len(list(await session.scalars(select(Channel).where(Channel.kind != "unset"))))
        pending_proposals = len(
            list(await session.scalars(select(Proposal).where(Proposal.status == "pending")))
        )

    due = await due_jobs()
    lines.append(f"bot admins: {admin_count}  watched channels: {channel_count}")
    lines.append(f"pending proposals: {pending_proposals}  jobs due: {len(due)} {due if due else ''}".strip())

    for job_name in ("ingest_tick", "lifecycle_tick", "project_agent_tick"):
        last = await ledger_tail(job_name, limit=1)
        if not last:
            lines.append(f"{job_name}: no runs yet")
            continue
        run = last[0]
        if run.outcome == "success":
            lines.append(f"{job_name}: ✅ last ran {_isoformat(run.finished_at)}")
        elif run.outcome == "failure":
            lines.append(f"{job_name}: ❌ failed {_isoformat(run.finished_at)} - {run.error}")
        else:
            lines.append(f"{job_name}: ⏳ started {_isoformat(run.started_at)}, still running")

    return "\n".join(lines)


# Everything is stored in UTC; `/health` is read by a Bot Admin on their
# phone in Delhi, so it renders in IST. A fixed offset rather than
# ZoneInfo("Asia/Kolkata") on purpose: IST has no DST, and the slim
# Docker base image ships no tzdata.
IST = timezone(timedelta(hours=5, minutes=30))


def _isoformat(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    # SQLite drops the tzinfo on round-trip; those timestamps were
    # written as UTC, so label them before converting (without this the
    # times would silently shift by 5.5 hours the wrong way).
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")


async def link(cmd: LinkCommand, requested_by: str) -> str:
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /link."
    member_title = cmd.member_ref.strip("[]")
    async with get_session() as session:
        row = await session.get(MembersRegistry, cmd.sender_ref)
        if row is None:
            session.add(
                MembersRegistry(wa_identity=cmd.sender_ref, member_title=member_title, linked_by=requested_by)
            )
        else:
            row.member_title = member_title
            row.linked_by = requested_by
        await session.commit()
    return f"Linked {cmd.sender_ref} to [[{member_title}]]."


async def handle_command(
    channel_jid: str, sender: str, text: str, vault: VaultClient | None = None
) -> str | None:
    """Route a parsed command to its handler. `vault` defaults to
    `default_vault_client()` (Lapis if configured, else a local
    directory) - pass a real client in tests."""
    cmd: Command | None = parse_command(text)
    vault = vault or default_vault_client()

    if cmd is None:
        return await continue_setup_session(channel_jid, text, vault)
    if isinstance(cmd, SetupCommand):
        return await setup(channel_jid, sender, cmd, vault)
    if isinstance(cmd, UnwatchCommand):
        return await unwatch(channel_jid, sender, target_jid=cmd.jid)
    if isinstance(cmd, StatusCommand):
        return await status(channel_jid)
    if isinstance(cmd, LinkCommand):
        return await link(cmd, sender)
    if isinstance(cmd, HealthCommand):
        return await health()
    if isinstance(cmd, ChannelsCommand):
        return await channels_report(sender)
    if isinstance(cmd, IngestCommand):
        return await trigger_ingest(sender)
    raise AssertionError(f"unhandled command type: {cmd!r}")


async def resolve_sender(wa_identity: str) -> MembersRegistry | None:
    """Look up a WhatsApp identity in the registry - already-linked
    senders skip fuzzy matching entirely."""
    async with get_session() as session:
        return await session.get(MembersRegistry, wa_identity)


async def _admin_channel() -> str | None:
    coordis = await _channel_by_kind("coordis")
    return coordis.jid if coordis else None


async def _logs_channel() -> str | None:
    logs = await _channel_by_kind("logs")
    return logs.jid if logs else None


async def notify_logs(text: str) -> bool:
    """Post `text` to the logs channel (`/setup logs`), if one is set
    up - the *only* place debug logging and error reporting go (never
    coordis, which is reserved for Proposals a Bot Admin needs to act
    on with a 👍). Used for proactive alerts like a scheduled job
    failing (see main.py's `_notify_job_failure`) so a Bot Admin finds
    out from WhatsApp instead of having to think to check
    `docker compose logs` or `/health`. Returns whether it could
    actually be sent."""
    channel = await _logs_channel()
    if channel is None:
        return False
    await send(channel, text)
    return True


async def propose_identity_link(
    wa_identity: str,
    display_name: str,
    cms_client: CmsClientProtocol | None = None,
) -> str:
    """Unknown sender -> fuzzy match against CMS member titles -> Proposal
    to Bot Admins. No match proposes creating a member page instead of
    guessing (Spec: Gateway identity)."""
    already = await resolve_sender(wa_identity)
    if already is not None:
        return f"{wa_identity} is already linked to [[{already.member_title}]]."

    cms_client = cms_client or default_cms_client()
    candidates: list[MemberRecord] = await cms_client.list_members()
    match = fuzzy_match_member(display_name, candidates)

    expires_at = datetime.now(UTC) + timedelta(hours=settings.proposal_expiry_hours)
    admin_channel = await _admin_channel()

    if match is not None:
        payload = json.dumps(
            {"wa_identity": wa_identity, "member_title": match.title, "cms_member_id": match.cms_id}
        )
        text = f"Link *{display_name}* to [[{match.title}]]? \U0001f44d"
        kind = "link_member"
    else:
        payload = json.dumps({"wa_identity": wa_identity, "display_name": display_name})
        text = f"No member found for *{display_name}* - create a member page and link it? \U0001f44d"
        kind = "create_member"

    async with get_session() as session:
        proposal = Proposal(
            channel=admin_channel or "unknown",
            kind=kind,
            payload=payload,
            expires_at=expires_at,
        )
        session.add(proposal)
        await session.commit()
        proposal_id = proposal.id

    if admin_channel:
        result = await send(admin_channel, text)
        async with get_session() as session:
            row = await session.get(Proposal, proposal_id)
            if row is not None:
                row.message_id = result.message_id
                await session.commit()

    return text


async def propose_wiki_write(
    channel_jid: str, path: str, section: str, line: str, preview_note: str | None = None
) -> str:
    """Any Chat Agent write is a Proposal (Spec: Chat Agent) - the bot
    posts the exact change and waits for a thumbs-up rather than writing
    directly. `line` is the literal text that will be appended to
    `section` in `path` on confirm."""
    expires_at = datetime.now(UTC) + timedelta(hours=settings.proposal_expiry_hours)
    payload = json.dumps({"path": path, "section": section, "line": line})
    text = preview_note or f"I'll add this to {path} ({section}):\n> {line}\n\n\U0001f44d to confirm."

    async with get_session() as session:
        proposal = Proposal(channel=channel_jid, kind="wiki_write", payload=payload, expires_at=expires_at)
        session.add(proposal)
        await session.commit()
        proposal_id = proposal.id

    result = await send(channel_jid, text)
    async with get_session() as session:
        row = await session.get(Proposal, proposal_id)
        if row is not None:
            row.message_id = result.message_id
            await session.commit()
    return text


async def confirm_proposal(proposal_id: int, confirmed_by: str, vault: VaultClient | None = None) -> str:
    """Execute a pending Proposal (Spec: 👍 within 24h executes it). The
    kind of proposal determines the effect; unknown kinds are refused
    rather than silently ignored."""
    async with get_session() as session:
        proposal = await session.get(Proposal, proposal_id)
        if proposal is None:
            return "No such proposal."
        if proposal.status != "pending":
            return f"Proposal already {proposal.status}."
        expires_at = proposal.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)  # SQLite drops tz on round-trip
        if expires_at and datetime.now(UTC) > expires_at:
            proposal.status = "expired"
            await session.commit()
            return "That proposal expired."

        data = json.loads(proposal.payload)
        kind = proposal.kind

        if kind == "link_member":
            wa_identity = data["wa_identity"]
            member_title = data["member_title"]
            cms_id = data.get("cms_member_id")
            existing = await session.get(MembersRegistry, wa_identity)
            if existing is None:
                session.add(
                    MembersRegistry(
                        wa_identity=wa_identity,
                        member_title=member_title,
                        cms_member_id=cms_id,
                        linked_by=confirmed_by,
                    )
                )
            else:
                existing.member_title = member_title
                existing.cms_member_id = cms_id
                existing.linked_by = confirmed_by
            reply = f"Linked to [[{member_title}]]."
        elif kind == "create_member":
            vault = vault or default_vault_client()
            title = data["display_name"]
            await vault.create(f"people/{slugify(title)}.md", render_new_member_page(title))
            session.add(
                MembersRegistry(
                    wa_identity=data["wa_identity"], member_title=title, linked_by=confirmed_by
                )
            )
            reply = f"Created [[{title}]] and linked."
        elif kind == "wiki_write":
            vault = vault or default_vault_client()
            path, section, line = data["path"], data["section"], data["line"]
            read_result = await vault.read(path)
            page = append_lines(parse_page(read_result.content), section, [line])
            await vault.write(path, dump_page(page), base_revision=read_result.revision)
            reply = f"Added to {path} ({section})."
        else:
            return f"Unknown proposal kind: {kind}"

        proposal.status = "confirmed"
        proposal.resolved_at = datetime.now(UTC)
        proposal.resolved_by = confirmed_by
        await session.commit()

    return reply


_THUMBS_UP = {"\U0001f44d", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿"}


async def handle_reaction(reactor: str, message_id: str | None, emoji: str | None) -> str | None:
    """A 👍 from a Bot Admin within 24h executes the reacted-to Proposal
    (Spec: Chat Agent Proposal flow, reused for identity link proposals).
    Distinguishing the initiative lead from a Bot Admin needs the wiki's
    `lead:` frontmatter, which isn't wired in here yet - only Bot Admins
    can confirm via reaction for now; lead-confirmation lands with the
    Phase 5 Chat Agent ticket that already depends on this one.
    """
    if not message_id or emoji not in _THUMBS_UP:
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
    """Scheduler job body (Phase 3): expired proposals are dropped with a
    one-line notice, never silently forgotten."""
    now = now or datetime.now(UTC)
    async with get_session() as session:
        pending = await session.scalars(select(Proposal).where(Proposal.status == "pending"))
        expired = [p for p in pending if p.expires_at and _aware(p.expires_at) < now]
        for p in expired:
            p.status = "expired"
        await session.commit()
        channels_and_ids = [(p.channel, p.id) for p in expired]

    for channel, proposal_id in channels_and_ids:
        if channel and channel != "unknown":
            await send(channel, f"Proposal #{proposal_id} expired without a \U0001f44d.")
    return len(channels_and_ids)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def request_history(channel: str, count: int) -> int:
    """Backfill: ask gowa for stored history and buffer whatever we don't
    already have, flagged `event_type="message.backfill"` so summaries can
    say "(from history)" (Spec: WhatsApp Agent ingestion)."""
    messages = await _client.get_chat_messages(channel, limit=count)
    added = 0
    for m in messages:
        wrapped = wrap_backfilled_message(m)
        message_id = wrapped["payload"]["id"]
        if not message_id:
            continue
        async with get_session() as session:
            existing = await session.scalar(
                select(MessageBuffer).where(MessageBuffer.message_id == message_id)
            )
            if existing is not None:
                continue
            session.add(
                MessageBuffer(
                    message_id=message_id,
                    channel=channel,
                    event_type="message.backfill",
                    payload=json.dumps(wrapped),
                )
            )
            await session.commit()
            added += 1
    return added


async def react(message_id: str, channel: str, emoji: str) -> None:
    """Bot-sent reaction (e.g. the ✅ confirming an executed write - Spec:
    Chat Agent). Not logged to outbound_log since it isn't a text send."""
    await _client.react(message_id, channel, emoji)
