"""SQLite schema and session factory. Phase 0 tables only —
`threads`, `members_registry`, etc. are added by their owning phases
(see docs/Spec - Watcher v1.md, "Shape" -> State).
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from shared.config import settings


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MessageBuffer(Base):
    """Raw inbound events, deduped by gowa message id (ADR-0002: gowa is
    the source of truth, this table is a processing queue, not a copy)."""

    __tablename__ = "messages_buffer"
    __table_args__ = (UniqueConstraint("message_id", name="uq_messages_buffer_message_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String, index=True)
    channel: Mapped[str] = mapped_column(String, index=True)
    event_type: Mapped[str] = mapped_column(String)
    payload: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    processed: Mapped[bool] = mapped_column(default=False)


class Channel(Base):
    """Allowlist. Populated by /setup in Phase 2 — empty is valid in Phase 0."""

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    jid: Mapped[str] = mapped_column(String, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String, default="unset")
    initiative: Mapped[str | None] = mapped_column(String, nullable=True)
    cursor: Mapped[str | None] = mapped_column(String, nullable=True)


class OutboundLog(Base):
    """Every send, mirrored to meta/audit/<yyyy-mm>.md by the wiki layer
    once it exists (Phase 1+); this table is the source of truth for it."""

    __tablename__ = "outbound_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String, index=True)
    text: Mapped[str] = mapped_column(Text)
    reply_to: Mapped[str | None] = mapped_column(String, nullable=True)
    message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Job(Base):
    """Registered per-process (Spec: "No workflow DSL") - this row is the
    ledger anchor and dependency-rule record, not the source of truth for
    what a job does."""

    __tablename__ = "jobs"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    lock_key: Mapped[str | None] = mapped_column(String, nullable=True)
    max_retries: Mapped[int] = mapped_column(default=0)


class Run(Base):
    """One row per job execution attempt (ADR-0007: waits are rows)."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_name: Mapped[str] = mapped_column(String, index=True)
    attempt: Mapped[int] = mapped_column(default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)  # success | failure
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


_engine = create_async_engine(settings.database_url)
async_session = async_sessionmaker(_engine, expire_on_commit=False)


async def init_db() -> None:
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_session() -> AsyncSession:
    return async_session()
