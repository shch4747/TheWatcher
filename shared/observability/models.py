"""Observability tables (ADR-0013), on the same `Base`/SQLite file as
everything else so Grafana needs one datasource and can join these to
`runs`, `channels` and `model_calls`.

Imported lazily by `shared.db.init_db` (a module-level import here would
be a cycle: these models import `Base` from `shared.db`).
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class IngestRun(Base):
    """One row per ingest tick that actually processed something. No-op
    ticks (the common case - the tick fires every 2 minutes) write
    nothing, so this table is a log of real work, not of wake-ups."""

    __tablename__ = "obs_ingest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # `runs.id` of the scheduler run this happened under, when there is
    # one (`/ingest` and tests call the tick directly).
    scheduler_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forced: Mapped[bool] = mapped_column(default=False)

    channels_total: Mapped[int] = mapped_column(default=0)
    channels_with_work: Mapped[int] = mapped_column(default=0)
    messages: Mapped[int] = mapped_column(default=0)
    threads_created: Mapped[int] = mapped_column(default=0)
    threads_updated: Mapped[int] = mapped_column(default=0)
    threads_revived: Mapped[int] = mapped_column(default=0)
    chatter: Mapped[int] = mapped_column(default=0)
    unassigned: Mapped[int] = mapped_column(default=0)

    model_calls: Mapped[int] = mapped_column(default=0)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    outcome: Mapped[str | None] = mapped_column(String, nullable=True)  # success | failure
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class IngestChannelRun(Base):
    """One row per channel per ingest run - "time taken per channel",
    with the token/cost split by phase (ADR-0012's two calls:
    classification assigns messages to threads, summarisation rewrites a
    thread's title/summary/items).

    The per-phase columns duplicate what could be derived from
    `model_calls`; they are denormalised on purpose so the WhatsApp
    report and the Grafana panels are single-row reads."""

    __tablename__ = "obs_ingest_channel_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ingest_run_id: Mapped[int] = mapped_column(Integer, index=True)
    channel_jid: Mapped[str] = mapped_column(String, index=True)
    channel_title: Mapped[str | None] = mapped_column(String, nullable=True)
    channel_kind: Mapped[str | None] = mapped_column(String, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Why nothing happened: "not-ready" (batch thresholds), "excluded"
    # (logs channel), "no-channel". NULL means the batch ran.
    skipped_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    messages: Mapped[int] = mapped_column(default=0)
    threads_created: Mapped[int] = mapped_column(default=0)
    threads_updated: Mapped[int] = mapped_column(default=0)
    threads_revived: Mapped[int] = mapped_column(default=0)
    chatter: Mapped[int] = mapped_column(default=0)
    unassigned: Mapped[int] = mapped_column(default=0)

    classification_calls: Mapped[int] = mapped_column(default=0)
    classification_input_tokens: Mapped[int] = mapped_column(default=0)
    classification_output_tokens: Mapped[int] = mapped_column(default=0)
    classification_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    summarisation_calls: Mapped[int] = mapped_column(default=0)
    summarisation_input_tokens: Mapped[int] = mapped_column(default=0)
    summarisation_output_tokens: Mapped[int] = mapped_column(default=0)
    summarisation_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    model_calls: Mapped[int] = mapped_column(default=0)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class VaultOp(Base):
    """Every mutation the agents make to the wiki (ADR-0013). Content is
    not copied here - Lapis already versions it, and copying every page
    body would dwarf the rest of the database. The hash and byte count
    are enough to see churn, and the revisions are enough to line a row
    up with Lapis's own history."""

    __tablename__ = "obs_vault_ops"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    path: Mapped[str] = mapped_column(String, index=True)
    operation: Mapped[str] = mapped_column(String)  # write | delete
    outcome: Mapped[str] = mapped_column(String, index=True)  # ok | conflict | error

    base_revision: Mapped[str | None] = mapped_column(String, nullable=True)
    new_revision: Mapped[str | None] = mapped_column(String, nullable=True)
    content_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    channel_jid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    phase: Mapped[str | None] = mapped_column(String, nullable=True)
    job_name: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
