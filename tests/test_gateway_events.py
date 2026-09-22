"""The gowa payload fields ingestion depends on beyond id/body/from:
the sender's display name and the time the message was actually sent
(ADR-0012 - timeline lines carry both). Shapes verified against gowa's
docs/webhook-payload.md."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from agents.wa_agent import interface as wa_agent
from shared.db import MessageBuffer, get_session
from shared.gateway.events import parse_gowa_event, parse_gowa_timestamp, wrap_backfilled_message

from tests.gowa_payloads import message_event


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

    messages = await wa_agent._unprocessed_messages(channel)
    assert len(messages) == 1
    m = messages[0]
    assert m.sender_name == "Aira J"
    assert m.when == datetime(2026, 9, 18, 10, 30, tzinfo=UTC)
    assert m.when != m.received_at


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

    m = (await wa_agent._unprocessed_messages(channel))[0]
    assert m.when == m.received_at
    assert datetime.now(UTC) - m.when < timedelta(minutes=5)
