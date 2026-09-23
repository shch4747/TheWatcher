"""SQLite schema and session factory. Phase 0 tables only —
`threads`, `members_registry`, etc. are added by their owning phases
(see docs/Spec - Watcher v1.md, "Shape" -> State).
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint, event
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
    ledger").

    The attribution columns (`phase`, `channel_jid`, `ingest_run_id`,
    `job_name`) are filled from the ambient context rather than passed
    by callers - see `shared.observability.context`. They were added
    after this table shipped, so `shared.observability.migrate` ALTERs
    them onto existing databases (there is no Alembic here).
    """

    __tablename__ = "model_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    tier: Mapped[str] = mapped_column(String)  # decision | worker | mentor
    model_name: Mapped[str] = mapped_column(String)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float | None] = mapped_column(nullable=True)
    # reported (provider told us) | computed (priced from tokens) |
    # estimated (flat rate) | unknown - so a dashboard can distinguish a
    # measured cost from a guess. See shared.observability.cost.
    cost_source: Mapped[str | None] = mapped_column(String, nullable=True)
    job_name: Mapped[str | None] = mapped_column(String, nullable=True)
    phase: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    channel_jid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    ingest_run_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    duration_ms: Mapped[int | None] = mapped_column(nullable=True)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)  # ok | error
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


_engine = create_async_engine(settings.database_url)
async_session = async_sessionmaker(_engine, expire_on_commit=False)


@event.listens_for(_engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """WAL lets a reader (Grafana, querying this same file through the
    SQLite datasource plugin) run concurrently with the app's writes;
    in the default rollback-journal mode they block each other and
    Grafana's polling would intermittently fail the app's commits.
    Harmless on non-SQLite backends because the listener no-ops."""
    if not settings.database_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


async def init_db() -> None:
    # Imported here, not at module scope: the observability models
    # import `Base` from this module, so a top-level import would be a
    # cycle. They must be imported before create_all or their tables
    # aren't registered on the metadata.
    from shared.observability import models as _obs_models  # noqa: F401
    from shared.observability.migrate import ensure_columns

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all only ever CREATEs; columns added to a table that
        # already exists in a deployed database need an explicit ALTER.
        await ensure_columns(conn)


def get_session() -> AsyncSession:
    return async_session()
