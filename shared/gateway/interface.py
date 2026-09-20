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
from pathlib import Path

import httpx
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
from shared.gateway.commands import (
    Command,
    LinkCommand,
    SetupCommand,
    StatusCommand,
    UnwatchCommand,
    is_group_jid,
    parse_command,
)
from shared.gateway.gowa_client import GowaClient
from shared.wiki.interface import (
    LocalDirClient,
    VaultClient,
    render_new_channel_page,
    render_new_event_page,
    render_new_project_page,
    slugify,
)

_client = GowaClient()

SETUP_STEPS = ["lead", "brief", "timeline"]


class SendResult(BaseModel):
    message_id: str | None


def verify_signature(body: bytes, signature: str | None) -> bool:
    """HMAC-SHA256 over the raw body, compared with the gowa webhook secret."""
    if not signature:
        return False
    expected = hmac.new(settings.gowa_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


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
    message_id = payload.get("message", {}).get("id") or payload.get("id")
    channel = payload.get("from") or payload.get("chat_id") or "unknown"
    event_type = payload.get("event", "message")
    sender = payload.get("sender") or payload.get("message", {}).get("sender")
    text = payload.get("message", {}).get("text", "")
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
                await session.commit()
                return True
            return False  # processed messages are immutable; batch cutter resets the quiet timer (Phase 4)
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
        # for a batch (Spec: Gateway commands).
        reply = await handle_command(channel, sender, text)
        if reply:
            await send(channel, reply)
        async with get_session() as session:
            row = await session.get(MessageBuffer, buffered.id)
            if row is not None:
                row.processed = True
                await session.commit()
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


async def _initiative_page_exists(vault: VaultClient, kind: str, slug: str) -> bool:
    path = f"{'projects' if kind == 'project' else 'events'}/{slug}.md"
    try:
        await vault.read(path)
        return True
    except (FileNotFoundError, OSError, httpx.HTTPStatusError):
        return False


async def setup(channel_jid: str, requested_by: str, cmd: SetupCommand, vault: VaultClient) -> str:
    """`/setup [kind] [<Title>]`. Singleton kinds refuse a second channel;
    project/event with a missing initiative page opens a setup_session
    asking lead/brief/timeline instead of creating a half-formed page."""
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /setup."

    if cmd.kind in ("coordis", "exes", "research", "all"):
        existing = await _channel_by_kind(cmd.kind)
        if existing is not None and existing.jid != channel_jid:
            return f"A {cmd.kind} channel is already set up; refusing a second one."
        await _upsert_channel(channel_jid, cmd.kind, cmd.title, initiative=None)
        return "watching"

    if cmd.kind == "other":
        await _upsert_channel(channel_jid, cmd.kind, cmd.title, initiative=None)
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
    await vault.write(f"channels/{slug}.md", channel_content, base_revision="")
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
        render_new_project_page(title, lead=lead_name)
        if kind == "project"
        else render_new_event_page(title, lead=lead_name)
    )
    if brief:
        page = page.replace("## Brief\n\n", f"## Brief\n{brief}\n\n", 1)
    await vault.write(f"{'projects' if kind == 'project' else 'events'}/{slug}.md", page, base_revision="")

    await _upsert_channel(channel_jid, kind, title, initiative=title)
    channel_content = render_new_channel_page(title, kind, initiative=title)
    await vault.write(f"channels/{slug}.md", channel_content, base_revision="")
    return f'"{title}" created (timeline: {timeline}). watching'


async def unwatch(channel_jid: str, requested_by: str) -> str:
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /unwatch."
    async with get_session() as session:
        channel = await session.scalar(select(Channel).where(Channel.jid == channel_jid))
        if channel is None:
            return "This channel wasn't being watched."
        channel.kind = "unset"
        channel.initiative = None
        await session.commit()
    return "No longer watching this channel."


async def status(channel_jid: str) -> str:
    channel = await get_channel(channel_jid)
    if channel is None or channel.kind == "unset":
        return "Not watching this channel."
    return f"kind={channel.kind} initiative={channel.initiative or '-'} cursor={channel.cursor or '-'}"


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
    """Route a parsed command to its handler. `vault` defaults to a
    process-local LocalDirClient rooted at ./vault for interactive/manual
    use - pass a real client in tests and in the deployed app."""
    cmd: Command | None = parse_command(text)
    vault = vault or LocalDirClient(_default_vault_root())

    if cmd is None:
        return await continue_setup_session(channel_jid, text, vault)
    if isinstance(cmd, SetupCommand):
        return await setup(channel_jid, sender, cmd, vault)
    if isinstance(cmd, UnwatchCommand):
        return await unwatch(channel_jid, sender)
    if isinstance(cmd, StatusCommand):
        return await status(channel_jid)
    if isinstance(cmd, LinkCommand):
        return await link(cmd, sender)
    raise AssertionError(f"unhandled command type: {cmd!r}")


def _default_vault_root() -> Path:
    return Path(settings.vault_root)
