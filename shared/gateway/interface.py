"""Gateway interface (ADR-0003): the only entry/exit point for WhatsApp.
Every other package calls these functions, never `gowa_client` or the DB
tables directly: webhook intake and edge filtering (allowlist, DMs, admin
commands before allowlisting), send, the message buffer (`buffer.py`),
commands (`/setup`, `/link`, ...), and the Member Registry.

Proposals are not here - they belong to the WhatsApp Agent. The Gateway
delivers reactions to whatever registered a reaction hook, and `/link`
and a confirmed identity Proposal share `link_member`.
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

from shared.config import settings
from shared.db import (
    BotAdmin,
    Channel,
    MembersRegistry,
    MessageBuffer,
    OutboundLog,
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
from shared.scheduler.interface import due_jobs, ledger_tail, registered_jobs, run_job
from shared.wiki.interface import (
    LapisClient,
    PageExists,
    ThreadStore,
    VaultClient,
    check_vault_connection,
    default_vault_client,
    initiative_page_path,
    read_if_exists,
    render_new_event_page,
    render_new_project_page,
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


# (reactor, reacted-to message id, emoji) - Proposal confirmation lives
# in the WhatsApp Agent; the Gateway only delivers the reaction.
ReactionHook = Callable[[str, str | None, str | None], Awaitable[object]]
_reaction_hooks: list[ReactionHook] = []

# Extra `/health` lines from modules the Gateway can't import (e.g. the
# WhatsApp Agent's pending Proposal count).
HealthLine = Callable[[], Awaitable[str]]
_health_lines: list[HealthLine] = []


def register_reaction_hook(hook: ReactionHook) -> None:
    """Called for every reaction in an allowlisted group. Same failure
    policy as message hooks."""
    _reaction_hooks.append(hook)


def register_health_line(line: HealthLine) -> None:
    _health_lines.append(line)


def clear_hooks() -> None:
    """Test-only: forget every registered message/reaction hook and
    health line."""
    _message_hooks.clear()
    _reaction_hooks.clear()
    _health_lines.clear()


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
        for reaction_hook in _reaction_hooks:
            try:
                await reaction_hook(sender, event.reacted_message_id, event.reaction_emoji)
            except Exception:  # noqa: BLE001 - a broken hook must not break intake
                logger.exception("reaction hook %r failed for channel %s", reaction_hook, channel)
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


_DEFAULT_KIND_TITLES = {
    "coordis": "Coordis",
    "exes": "Exes",
    "research": "Research",
    "all": "All",
    "logs": "Logs",
    "other": "Other",
}


async def _register_channel(
    vault: VaultClient, jid: str, kind: str, title: str, initiative: str | None
) -> None:
    """Allowlist the Channel and make sure it has a Channel Page. A repeat
    `/setup` never replaces an existing page (or its Notes); one that
    retitles the Channel moves its Threads and page to the new directory
    instead of orphaning them."""
    old = await get_channel(jid)
    await _upsert_channel(jid, kind, title, initiative)
    new = await get_channel(jid)
    assert new is not None
    if old is not None:
        left_behind = await ThreadStore.relocate(vault, old, new)
        if left_behind:
            logger.warning("retitling %s left pages in place (targets existed): %s", jid, left_behind)
    await ThreadStore(vault, new).ensure_channel_page()


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
        await _register_channel(vault, channel_jid, cmd.kind, title, initiative=None)
        return "watching"

    # project / event
    if not cmd.title:
        return f"/setup {cmd.kind} needs a title: /setup {cmd.kind} <Title>"

    if await read_if_exists(vault, initiative_page_path(cmd.kind, cmd.title)) is None:
        async with get_session() as session:
            session.add(
                SetupSession(channel=channel_jid, kind=cmd.kind, title=cmd.title, requested_by=requested_by)
            )
            await session.commit()
        return f'Setting up "{cmd.title}" as a new {cmd.kind}. Who is the lead? (reply with their name)'

    await _register_channel(vault, channel_jid, cmd.kind, cmd.title, initiative=cmd.title)
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

    lead_name = lead or requested_by
    render = render_new_project_page if kind == "project" else render_new_event_page
    try:
        page = render(title, lead=lead_name, brief=brief or "")
        await vault.create(initiative_page_path(kind, title), page)
    except PageExists:
        pass  # created by someone else meanwhile - link to it, never replace it
    await _register_channel(vault, channel_jid, kind, title, initiative=title)
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

    # Same predicate default_decision_client() uses to pick Jev.
    if settings.jev_api_key:
        decision_line = f"decision model: Jev ({settings.jev_base_url or 'hosted'})"
    else:
        decision_line = "decision model: Worker-backed (no JEV_API_KEY)"
    lines.append(decision_line)
    lines.append(f"worker model: {settings.worker_model_name}")
    lines.append(f"mentor model: {settings.mentor_model_name}")

    async with get_session() as session:
        admin_count = len(list(await session.scalars(select(BotAdmin))))
        channel_count = len(list(await session.scalars(select(Channel).where(Channel.kind != "unset"))))

    due = await due_jobs()
    lines.append(f"bot admins: {admin_count}  watched channels: {channel_count}")
    lines.append(f"jobs due: {len(due)} {due if due else ''}".strip())
    for health_line in _health_lines:
        try:
            lines.append(await health_line())
        except Exception as exc:  # noqa: BLE001 - /health reports, never fails
            lines.append(f"❌ {exc}")

    for job_name in registered_jobs():
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
    await link_member(cmd.sender_ref, member_title, linked_by=requested_by)
    return f"Linked {cmd.sender_ref} to [[{member_title}]]."


async def link_member(
    wa_identity: str, member_title: str, cms_member_id: str | None = None, linked_by: str | None = None
) -> None:
    """Link (or re-link) a WhatsApp identity to a member in the Member
    Registry. `/link` and confirmed identity Proposals both land here."""
    async with get_session() as session:
        row = await session.get(MembersRegistry, wa_identity)
        if row is None:
            session.add(
                MembersRegistry(
                    wa_identity=wa_identity,
                    member_title=member_title,
                    cms_member_id=cms_member_id,
                    linked_by=linked_by,
                )
            )
        else:
            row.member_title = member_title
            row.linked_by = linked_by
            if cms_member_id is not None:
                row.cms_member_id = cms_member_id
        await session.commit()


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
