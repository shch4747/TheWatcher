"""Gateway interface (ADR-0003): the only entry/exit point for WhatsApp.
Every other package calls these functions, never `gowa_client` or the DB
tables directly. Phase 0 implements webhook intake + send only; commands,
allowlisting, identity and history live in Phase 2 (see docs/Plan).
"""
from __future__ import annotations

import hashlib
import hmac
import json

from pydantic import BaseModel
from sqlalchemy import select

from shared.config import settings
from shared.db import MessageBuffer, OutboundLog, get_session
from shared.gateway.gowa_client import GowaClient

_client = GowaClient()


class SendResult(BaseModel):
    message_id: str | None


def verify_signature(body: bytes, signature: str | None) -> bool:
    """HMAC-SHA256 over the raw body, compared with the gowa webhook secret."""
    if not signature:
        return False
    expected = hmac.new(settings.gowa_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def receive_webhook(raw_body: bytes, signature: str | None) -> bool:
    """Verify, dedupe by message id, and buffer a raw gowa event.
    Returns False (and buffers nothing) for a bad signature or a duplicate.
    """
    if not verify_signature(raw_body, signature):
        return False

    payload = json.loads(raw_body)
    message_id = payload.get("message", {}).get("id") or payload.get("id")
    channel = payload.get("from") or payload.get("chat_id") or "unknown"
    event_type = payload.get("event", "message")
    if not message_id:
        return False

    async with get_session() as session:
        existing = await session.scalar(select(MessageBuffer).where(MessageBuffer.message_id == message_id))
        if existing is not None:
            return False
        session.add(
            MessageBuffer(
                message_id=message_id,
                channel=channel,
                event_type=event_type,
                payload=raw_body.decode(),
            )
        )
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
