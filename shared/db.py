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


def aware_utc(dt: datetime | None) -> datetime | None:
    """SQLite round-trips `DateTime(timezone=True)` as a naive datetime -
    every comparison against a fresh `datetime.now(UTC)` needs this first."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=UTC)


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
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    initiative: Mapped[str | None] = mapped_column(String, nullable=True)
    cursor: Mapped[str | None] = mapped_column(String, nullable=True)


class BotAdmin(Base):
    """Bot Admins can /setup before a channel is allowlisted (Spec: Gateway
    edge filtering lets admin commands through pre-allowlist)."""

    __tablename__ = "bot_admins"

    wa_identity: Mapped[str] = mapped_column(String, primary_key=True)


class SetupSession(Base):
    """A `/setup project <Title>` dialogue in progress when the initiative
    page doesn't exist yet (Spec: Gateway "opens a setup_session that asks
    for lead, brief, timeline in chat")."""

    __tablename__ = "setup_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)  # project | event
    title: Mapped[str] = mapped_column(String)
    requested_by: Mapped[str] = mapped_column(String)
    step: Mapped[str] = mapped_column(String, default="lead")  # lead -> brief -> timeline -> done
    lead: Mapped[str | None] = mapped_column(String, nullable=True)
    brief: Mapped[str | None] = mapped_column(String, nullable=True)
    timeline: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MembersRegistry(Base):
    """WhatsApp identity <-> wiki member title, keyed on CMS member id
    (ADR-0009). Admin `/link` writes here directly; fuzzy-match
    auto-linking goes through a Proposal first."""

    __tablename__ = "members_registry"

    wa_identity: Mapped[str] = mapped_column(String, primary_key=True)
    member_title: Mapped[str] = mapped_column(String)
    cms_member_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    linked_by: Mapped[str | None] = mapped_column(String, nullable=True)


class Proposal(Base):
    """A change the bot wants to make but won't without a 👍 (ADR-0007:
    waits are rows). `kind` + `payload` describe what to do on confirm;
    Phase 2's reaction-handling ticket adds the 👍 -> confirm_proposal
    wiring, Phase 5 reuses this same table for wiki-write proposals."""

    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)  # e.g. "link_member"
    payload: Mapped[str] = mapped_column(Text)  # JSON
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|confirmed|rejected|expired
    message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)


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


class ModelCall(Base):
    """Every model call, any tier, logged with tokens and cost (Spec:
    Models - "every model call is logged with tokens and cost to the run
    ledger")."""

    __tablename__ = "model_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    tier: Mapped[str] = mapped_column(String)  # decision | worker | mentor
    model_name: Mapped[str] = mapped_column(String)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float | None] = mapped_column(nullable=True)
    job_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Notice(Base):
    """Update Notice posted to an agent's Inbox (Spec: "Update Notices
    posted to the Inbox of the initiative's agent"). `inbox/<agent>.md`
    is a derived, read-only rendering of these rows."""

    __tablename__ = "notices"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String, index=True)
    channel: Mapped[str] = mapped_column(String)
    thread_slug: Mapped[str] = mapped_column(String)
    since_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


_engine = create_async_engine(settings.database_url)
async_session = async_sessionmaker(_engine, expire_on_commit=False)


async def init_db() -> None:
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_session() -> AsyncSession:
    return async_session()
