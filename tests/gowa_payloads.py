"""Builders for real gowa webhook payload shapes (verified against
https://github.com/aldinokemal/go-whatsapp-web-multidevice/blob/main/docs/webhook-payload.md,
2026-09-20). Every test that constructs a raw webhook body should use
these instead of hand-rolling the JSON, so a future gowa shape change
only needs fixing in one place.
"""
from __future__ import annotations


def message_event(
    message_id: str,
    chat_id: str,
    text: str = "",
    sender: str | None = None,
    replied_to_id: str | None = None,
    event: str = "message",
    sender_name: str | None = None,
    timestamp: str | None = None,
) -> dict:
    body: dict = {"id": message_id, "chat_id": chat_id, "body": text}
    if sender:
        body["from"] = sender
    if replied_to_id:
        body["replied_to_id"] = replied_to_id
    if sender_name:
        body["sender_display_name"] = sender_name
    if timestamp:
        body["timestamp"] = timestamp
    return {"event": event, "payload": body}


def reaction_event(
    event_message_id: str, chat_id: str, sender: str, emoji: str, reacted_message_id: str
) -> dict:
    return {
        "event": "message.reaction",
        "payload": {
            "id": event_message_id,
            "chat_id": chat_id,
            "from": sender,
            "reaction": emoji,
            "reacted_message_id": reacted_message_id,
        },
    }
