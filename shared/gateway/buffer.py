"""The message buffer (ADR-0002: gowa is the raw source of truth; this is
our processing queue of what it sent us). Owned by the Gateway: nothing
outside `shared/gateway` reads `messages_buffer` rows or gowa payloads -
consumers get `InboundMessage`s through `interface.py`.

What a consumer can rely on:

- `pending_messages(channel)` is every message not yet consumed, in
  arrival order: live messages, backfilled history (`backfilled=True`),
  and messages edited before they were consumed (`edited=True`, with the
  edited text). An edit also moves `received_at` forward, so a batch's
  quiet period really restarts.
- Everything else buffered for the channel - revoked messages,
  reactions, any other event type - is consumed on sight and never
  returned; it can't sit unprocessed forever.
- `mark_consumed(row_ids)` after the messages have been written; until
  then a retry sees the same set (ingestion is idempotent on this).
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import select, update

from shared.db import MessageBuffer, aware_utc, get_session
from shared.gateway.events import parse_gowa_event, parse_gowa_timestamp

INGESTED_EVENTS = ("message", "message.backfill", "message.edited")


class InboundMessage(BaseModel):
    row_id: int
    message_id: str
    channel: str
    received_at: datetime
    # When it was sent (gowa's timestamp) - for backfilled history, long
    # before received_at. Falls back to received_at when gowa gave none.
    sent_at: datetime
    text: str
    sender: str
    sender_name: str | None = None
    replied_to_id: str | None = None
    backfilled: bool = False
    edited: bool = False


def to_inbound(row: MessageBuffer) -> InboundMessage:
    event = parse_gowa_event(json.loads(row.payload))
    received_at = aware_utc(row.received_at) or datetime.now(UTC)
    return InboundMessage(
        row_id=row.id,
        message_id=row.message_id,
        channel=row.channel,
        received_at=received_at,
        sent_at=parse_gowa_timestamp(event.timestamp) or received_at,
        text=event.text,
        sender=event.sender or "unknown",
        sender_name=event.sender_name,
        replied_to_id=event.replied_to_id,
        backfilled=row.event_type == "message.backfill",
        edited=row.event_type == "message.edited",
    )


async def pending_messages(channel: str) -> list[InboundMessage]:
    async with get_session() as session:
        rows = list(
            await session.scalars(
                select(MessageBuffer)
                .where(MessageBuffer.channel == channel, MessageBuffer.processed.is_(False))
                .order_by(MessageBuffer.id)
            )
        )
        not_ingested = [r.id for r in rows if r.event_type not in INGESTED_EVENTS]
        if not_ingested:
            await session.execute(
                update(MessageBuffer).where(MessageBuffer.id.in_(not_ingested)).values(processed=True)
            )
            await session.commit()
    return [to_inbound(r) for r in rows if r.event_type in INGESTED_EVENTS]


async def mark_consumed(row_ids: Iterable[int]) -> None:
    ids = list(row_ids)
    if not ids:
        return
    async with get_session() as session:
        await session.execute(update(MessageBuffer).where(MessageBuffer.id.in_(ids)).values(processed=True))
        await session.commit()


async def get_message(message_id: str) -> InboundMessage | None:
    """A buffered message by gowa id."""
    async with get_session() as session:
        row = await session.scalar(select(MessageBuffer).where(MessageBuffer.message_id == message_id))
    return to_inbound(row) if row else None


async def get_messages(channel: str, since_id: str | None = None, limit: int = 100) -> list[InboundMessage]:
    async with get_session() as session:
        stmt = select(MessageBuffer).where(MessageBuffer.channel == channel)
        if since_id:
            anchor_stmt = select(MessageBuffer.id).where(MessageBuffer.message_id == since_id)
            anchor = await session.scalar(anchor_stmt)
            if anchor is not None:
                stmt = stmt.where(MessageBuffer.id > anchor)
        rows = await session.scalars(stmt.order_by(MessageBuffer.id).limit(limit))
    return [to_inbound(r) for r in rows]


async def get_context(message_id: str, before: int = 5, after: int = 5) -> list[InboundMessage]:
    """Messages around `message_id` in the same channel, in order."""
    async with get_session() as session:
        target = await session.scalar(select(MessageBuffer).where(MessageBuffer.message_id == message_id))
        if target is None:
            return []
        before_rows = await session.scalars(
            select(MessageBuffer)
            .where(MessageBuffer.channel == target.channel, MessageBuffer.id < target.id)
            .order_by(MessageBuffer.id.desc())
            .limit(before)
        )
        after_rows = await session.scalars(
            select(MessageBuffer)
            .where(MessageBuffer.channel == target.channel, MessageBuffer.id > target.id)
            .order_by(MessageBuffer.id)
            .limit(after)
        )
    ordered = list(reversed(list(before_rows))) + [target] + list(after_rows)
    return [to_inbound(r) for r in ordered]
