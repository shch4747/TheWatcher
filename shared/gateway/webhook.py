"""Webhook intake: verify, parse, then dispatch to a single-purpose handler."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from shared.config import settings
from shared.db import MessageBuffer, get_session
from shared.gateway.buffer import to_inbound
from shared.gateway.channels import is_allowlisted, is_bot_admin
from shared.gateway.commands import is_group_jid
from shared.gateway.events import parse_gowa_event
from shared.gateway.runtime import GatewayRuntime

logger = logging.getLogger(__name__)


def verify_signature(body: bytes, signature: str | None) -> bool:
    """HMAC-SHA256 over the raw body, checked against the current secret
    and, during a rotation window, the previous one too.
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


async def receive_webhook(raw_body: bytes, signature: str | None, runtime: GatewayRuntime) -> bool:
    """Verify, edge-filter, dedupe, and dispatch a raw gowa event."""
    if not verify_signature(raw_body, signature):
        return False

    payload = json.loads(raw_body)
    event = parse_gowa_event(payload)
    message_id = event.message_id
    channel = event.chat_id or "unknown"
    if not message_id:
        return False
    if not is_group_jid(channel):
        return False

    is_admin_command = bool(
        event.sender
        and event.text
        and event.text.strip().startswith("/")
        and await is_bot_admin(event.sender)
    )
    if not is_admin_command and not await is_allowlisted(channel):
        return False

    async with get_session() as session:
        existing = await session.scalar(select(MessageBuffer).where(MessageBuffer.message_id == message_id))
        if event.event_type in ("message.edited", "message.revoked") and existing is not None:
            if existing.processed:
                return False
            existing.payload = raw_body.decode()
            existing.event_type = event.event_type
            existing.received_at = datetime.now(UTC)
            await session.commit()
            return True
        if existing is not None:
            return False
        buffered = MessageBuffer(
            message_id=message_id,
            channel=channel,
            event_type=event.event_type,
            payload=raw_body.decode(),
        )
        session.add(buffered)
        await session.commit()

    if is_admin_command:
        await _dispatch_admin_command(runtime, channel, event.sender, event.text, buffered.id)
        return True
    if event.event_type == "message.reaction" and event.sender:
        await _dispatch_reaction(
            runtime, channel, event.sender, event.reacted_message_id, event.reaction_emoji
        )
        return True
    if event.event_type == "message":
        await _dispatch_message(runtime, buffered)
        return True
    return True


async def _dispatch_admin_command(
    runtime: GatewayRuntime, channel: str, sender: str | None, text: str, buffer_id: int
) -> None:
    from shared.gateway.command_handlers import handle_command

    assert sender is not None
    try:
        reply = await handle_command(channel, sender, text, runtime)
        if reply:
            await runtime.send(channel, reply)
    except Exception:  # noqa: BLE001 - logged, never crashes webhook intake
        logger.exception("admin command handling failed for %s in %s", text, channel)
    async with get_session() as session:
        row = await session.get(MessageBuffer, buffer_id)
        if row is not None:
            row.processed = True
            await session.commit()


async def _dispatch_reaction(
    runtime: GatewayRuntime,
    channel: str,
    sender: str,
    reacted_message_id: str | None,
    emoji: str | None,
) -> None:
    for reaction_hook in runtime.hooks.reaction_hooks:
        try:
            await reaction_hook(sender, reacted_message_id, emoji)
        except Exception:  # noqa: BLE001 - a broken hook must not break intake
            logger.exception("reaction hook %r failed for channel %s", reaction_hook, channel)


async def _dispatch_message(runtime: GatewayRuntime, buffered: MessageBuffer) -> None:
    message = to_inbound(buffered)
    for hook in runtime.hooks.message_hooks:
        try:
            await hook(message)
        except Exception:  # noqa: BLE001 - a broken hook must not break intake
            logger.exception("message hook %r failed for channel %s", hook, message.channel)
