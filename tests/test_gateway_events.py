"""The gowa payload fields ingestion depends on beyond id/body/from:
the sender's display name and the time the message was actually sent
(ADR-0012 - timeline lines carry both). Shapes verified against gowa's
docs/webhook-payload.md."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from shared.db import MessageBuffer, get_session
from shared.gateway import interface as gateway
from shared.gateway.events import parse_gowa_event, parse_gowa_timestamp, wrap_backfilled_message
from sqlalchemy import select

from tests.gowa_payloads import message_event, reaction_event


def test_sender_display_name_wins_over_legacy_push_name():
    event = parse_gowa_event(
        {
            "event": "message",
            "payload": {
                "id": "m1",
                "chat_id": "c@g.us",
                "from": "919000000001@s.whatsapp.net",
                "sender_display_name": "Aira J",
                "from_name": "aira (push)",
                "body": "hi",
            },
        }
    )
    assert event.sender_name == "Aira J"


def test_push_name_is_used_when_there_is_no_display_name():
    event = parse_gowa_event(
        {"event": "message", "payload": {"id": "m1", "from_name": "Rohan", "body": "hi"}}
    )
    assert event.sender_name == "Rohan"


def test_sender_name_is_none_when_gowa_sends_neither():
    event = parse_gowa_event({"event": "message", "payload": {"id": "m1", "body": "hi"}})
    assert event.sender_name is None


def test_parse_gowa_timestamp_handles_rfc3339_epoch_and_junk():
    rfc = parse_gowa_timestamp("2026-09-18T10:30:00Z")
    assert rfc == datetime(2026, 9, 18, 10, 30, tzinfo=UTC)

    offset = parse_gowa_timestamp("2026-09-18T16:00:00+05:30")
    assert offset == datetime(2026, 9, 18, 10, 30, tzinfo=UTC)
    assert offset.tzinfo is UTC  # always normalised, never left on a local offset

    epoch = parse_gowa_timestamp(1789000000)
    assert epoch is not None and epoch.tzinfo is UTC
    assert parse_gowa_timestamp("1789000000") == epoch

    naive = parse_gowa_timestamp("2026-09-18T10:30:00")
    assert naive is not None and naive.tzinfo is UTC  # assumed UTC, never naive

    assert parse_gowa_timestamp(None) is None
    assert parse_gowa_timestamp("") is None
    assert parse_gowa_timestamp("last Tuesday") is None


def test_backfilled_message_keeps_name_and_timestamp():
    wrapped = wrap_backfilled_message(
        {
            "id": "m1",
            "chat_jid": "c@g.us",
            "sender_jid": "919000000001@s.whatsapp.net",
            "sender_name": "Aira J",
            "content": "from history",
            "timestamp": "2026-09-18T10:30:00Z",
        }
    )
    event = parse_gowa_event(wrapped)
    assert event.event_type == "message.backfill"
    assert event.sender_name == "Aira J"
    assert event.text == "from history"
    assert parse_gowa_timestamp(event.timestamp) == datetime(2026, 9, 18, 10, 30, tzinfo=UTC)


async def test_buffered_message_uses_sent_time_not_received_time():
    """A backfilled message is buffered now but was sent long ago - the
    timeline must show when it was sent."""
    channel = "evt-chan@g.us"
    payload = message_event(
        "evt-m1", channel, "hi", sender="a@x", sender_name="Aira J", timestamp="2026-09-18T10:30:00Z"
    )
    async with get_session() as session:
        session.add(
            MessageBuffer(
                message_id="evt-m1", channel=channel, event_type="message", payload=json.dumps(payload)
            )
        )
        await session.commit()

    messages = await gateway.pending_messages(channel)
    assert len(messages) == 1
    m = messages[0]
    assert m.sender_name == "Aira J"
    assert m.sent_at == datetime(2026, 9, 18, 10, 30, tzinfo=UTC)
    assert m.sent_at != m.received_at


async def test_buffered_message_falls_back_to_received_time_without_a_timestamp():
    channel = "evt-chan-2@g.us"
    payload = message_event("evt-m2", channel, "hi", sender="a@x")
    async with get_session() as session:
        session.add(
            MessageBuffer(
                message_id="evt-m2", channel=channel, event_type="message", payload=json.dumps(payload)
            )
        )
        await session.commit()

    m = (await gateway.pending_messages(channel))[0]
    assert m.sent_at == m.received_at
    assert datetime.now(UTC) - m.sent_at < timedelta(minutes=5)


async def _buffer(channel: str, event: dict, event_type: str | None = None) -> None:
    async with get_session() as session:
        session.add(
            MessageBuffer(
                message_id=event["payload"]["id"], channel=channel,
                event_type=event_type or event["event"], payload=json.dumps(event),
            )
        )
        await session.commit()


async def test_a_message_edited_before_ingestion_is_ingested_with_its_new_text():
    """Regression: an edit relabelled the row `message.edited`, which the
    batch cutter never selected - the message was never ingested and
    stayed unprocessed forever."""
    channel = "evt-edit@g.us"
    await _buffer(channel, message_event("e1", channel, "meet at 5", sender="a@x"))
    await _buffer(channel, message_event("e2", channel, "ok", sender="b@x"))
    async with get_session() as session:
        row = await session.scalar(select(MessageBuffer).where(MessageBuffer.message_id == "e1"))
        row.event_type = "message.edited"
        edited = message_event("e1", channel, "meet at 6", sender="a@x", event="message.edited")
        row.payload = json.dumps(edited)
        await session.commit()

    pending = await gateway.pending_messages(channel)
    assert [(m.message_id, m.text, m.edited) for m in pending] == [
        ("e1", "meet at 6", True),
        ("e2", "ok", False),
    ]

    await gateway.mark_consumed(m.row_id for m in pending)
    assert await gateway.pending_messages(channel) == []


async def test_revoked_messages_and_reactions_are_consumed_not_returned():
    channel = "evt-noise@g.us"
    await _buffer(channel, message_event("n1", channel, "gone", sender="a@x"), event_type="message.revoked")
    await _buffer(channel, reaction_event("n2", channel, "a@x", "👍", "n0"))
    await _buffer(channel, message_event("n3", channel, "kept", sender="a@x"))

    assert [m.message_id for m in await gateway.pending_messages(channel)] == ["n3"]
    async with get_session() as session:
        rows = {r.message_id: r.processed for r in await session.scalars(
            select(MessageBuffer).where(MessageBuffer.channel == channel))}
    assert rows == {"n1": True, "n2": True, "n3": False}
