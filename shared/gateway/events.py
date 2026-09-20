"""Parses gowa's real webhook payload shape (verified against
https://github.com/aldinokemal/go-whatsapp-web-multidevice/blob/main/docs/webhook-payload.md,
2026-09-20 - the earlier assumed shape in this codebase, {"message":
{"id", "text", ...}}, does not match the real API and was never
verified against it before now).

Real shape:
    {"event": "message", "device_id": "...", "session_id": "...",
     "payload": {"id", "chat_id", "from", "body", "timestamp",
                 "replied_to_id"?, "reaction"?, "reacted_message_id"?}}

Every webhook event (and every row we later read back out of
`messages_buffer`) goes through this parser, so the field-name mapping
lives in exactly one place.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GowaEvent:
    event_type: str
    message_id: str | None
    chat_id: str | None
    sender: str | None
    text: str
    timestamp: str | None
    replied_to_id: str | None
    reaction_emoji: str | None
    reacted_message_id: str | None


def parse_gowa_event(raw: dict) -> GowaEvent:
    body = raw.get("payload", {})
    return GowaEvent(
        event_type=raw.get("event", "message"),
        message_id=body.get("id"),
        chat_id=body.get("chat_id"),
        sender=body.get("from"),
        text=body.get("body", ""),
        timestamp=body.get("timestamp"),
        replied_to_id=body.get("replied_to_id"),
        reaction_emoji=body.get("reaction"),
        reacted_message_id=body.get("reacted_message_id"),
    )


def wrap_backfilled_message(chat_message: dict) -> dict:
    """`GET /chat/{jid}/messages` (used by `request_history`) returns a
    *different* field shape than webhooks - `content`/`sender_jid`
    instead of `body`/`from`. Normalize a REST ChatMessage into the same
    on-disk shape as a webhook event, so `parse_gowa_event` (and
    everything downstream of `messages_buffer`) never needs to know
    which path a message arrived through.
    """
    return {
        "event": "message.backfill",
        "payload": {
            "id": chat_message.get("id"),
            "chat_id": chat_message.get("chat_jid"),
            "from": chat_message.get("sender_jid"),
            "body": chat_message.get("content", ""),
            "timestamp": chat_message.get("timestamp"),
        },
    }
