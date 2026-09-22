"""Per-run, per-channel ingest accounting (ADR-0013).

`main.py`'s tick used to loop channels and throw every `BatchResult`
away. It now runs inside `ingest_run()`, which times each channel, rolls
that channel's `model_calls` rows up by phase, and writes one
`obs_ingest_runs` row plus one `obs_ingest_channel_runs` row per
channel.

Rollups come from `model_calls` rather than from counters passed around
by hand: the rows are written anyway, they already carry the phase, and
reading them back means the dashboard and the WhatsApp report can never
disagree about what a run cost.
"""
from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from shared.db import ModelCall, get_session
from shared.observability import context
from shared.observability.models import IngestChannelRun, IngestRun

logger = logging.getLogger(__name__)


@dataclass
class PhaseTotals:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ChannelTotals:
    """One channel's slice of a run - what the report prints per line."""

    channel_jid: str
    channel_title: str | None
    channel_kind: str | None
    duration_ms: int = 0
    messages: int = 0
    threads_created: int = 0
    threads_updated: int = 0
    threads_revived: int = 0
    chatter: int = 0
    unassigned: int = 0
    skipped_reason: str | None = None
    error: str | None = None
    phases: dict[str, PhaseTotals] = field(default_factory=dict)

    def phase(self, name: str) -> PhaseTotals:
        return self.phases.get(name, PhaseTotals())

    @property
    def calls(self) -> int:
        return sum(p.calls for p in self.phases.values())

    @property
    def input_tokens(self) -> int:
        return sum(p.input_tokens for p in self.phases.values())

    @property
    def output_tokens(self) -> int:
        return sum(p.output_tokens for p in self.phases.values())

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float | None:
        costs = [p.cost_usd for p in self.phases.values() if p.cost_usd is not None]
        return sum(costs) if costs else None


async def _phase_totals(ingest_run_id: int, channel_jid: str) -> dict[str, PhaseTotals]:
    """Roll this channel's model_calls up by phase. Failed calls count
    towards `calls` (they burned a request) but contribute whatever
    tokens the provider reported, usually zero."""
    async with get_session() as session:
        rows = await session.execute(
            select(
                ModelCall.phase,
                func.count(),
                func.coalesce(func.sum(ModelCall.input_tokens), 0),
                func.coalesce(func.sum(ModelCall.output_tokens), 0),
                func.sum(ModelCall.cost_usd),
            )
            .where(ModelCall.ingest_run_id == ingest_run_id, ModelCall.channel_jid == channel_jid)
            .group_by(ModelCall.phase)
        )
        return {
            (phase or "other"): PhaseTotals(
                calls=calls,
                input_tokens=int(tokens_in),
                output_tokens=int(tokens_out),
                cost_usd=float(cost) if cost is not None else None,
            )
            for phase, calls, tokens_in, tokens_out, cost in rows
        }


class IngestRunRecorder:
    """Handed to the tick by `ingest_run()`. Not constructed directly."""

    def __init__(self, run_id: int, forced: bool):
        self.run_id = run_id
        self.forced = forced
        self.channels: list[ChannelTotals] = []
        self._current: ChannelTotals | None = None
        self._started = time.monotonic()

    @asynccontextmanager
    async def channel(self, channel: Any) -> AsyncIterator[ChannelTotals]:
        """Time one channel's batch and attribute its model calls to it.
        An exception inside the block is recorded on the row and
        re-raised, so a failing channel is visible rather than silently
        missing from the run."""
        totals = ChannelTotals(
            channel_jid=channel.jid,
            channel_title=channel.title,
            channel_kind=channel.kind,
        )
        self._current = totals
        self.channels.append(totals)
        started = time.monotonic()
        with context.scope(ingest_run_id=self.run_id, channel_jid=channel.jid):
            try:
                yield totals
            except Exception as exc:
                totals.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                totals.duration_ms = int((time.monotonic() - started) * 1000)
                totals.phases = await _phase_totals(self.run_id, channel.jid)
                await self._write_channel_row(totals)
                self._current = None

    def record(self, result: Any | None) -> None:
        """Attach a `BatchResult` (or None when the batch wasn't ready)
        to the channel currently being timed."""
        totals = self._current
        if totals is None:  # pragma: no cover - misuse
            return
        if result is None:
            totals.skipped_reason = "not-ready"
            return
        totals.messages = result.message_count
        totals.threads_created = len(result.threads_created)
        totals.threads_updated = len(result.threads_updated)
        totals.threads_revived = len(result.threads_revived)
        totals.chatter = result.chatter_count
        totals.unassigned = getattr(result, "unassigned", 0)
        if result.message_count and not result.model_calls:
            totals.skipped_reason = "excluded"  # e.g. the logs channel (NON_INGESTED_KINDS)

    @property
    def messages(self) -> int:
        return sum(c.messages for c in self.channels)

    async def _write_channel_row(self, totals: ChannelTotals) -> None:
        classification = totals.phase(context.CLASSIFICATION)
        summarisation = totals.phase(context.SUMMARISATION)
        try:
            async with get_session() as session:
                session.add(
                    IngestChannelRun(
                        ingest_run_id=self.run_id,
                        channel_jid=totals.channel_jid,
                        channel_title=totals.channel_title,
                        channel_kind=totals.channel_kind,
                        finished_at=datetime.now(UTC),
                        duration_ms=totals.duration_ms,
                        skipped_reason=totals.skipped_reason,
                        messages=totals.messages,
                        threads_created=totals.threads_created,
                        threads_updated=totals.threads_updated,
                        threads_revived=totals.threads_revived,
                        chatter=totals.chatter,
                        unassigned=totals.unassigned,
                        classification_calls=classification.calls,
                        classification_input_tokens=classification.input_tokens,
                        classification_output_tokens=classification.output_tokens,
                        classification_cost_usd=classification.cost_usd,
                        summarisation_calls=summarisation.calls,
                        summarisation_input_tokens=summarisation.input_tokens,
                        summarisation_output_tokens=summarisation.output_tokens,
                        summarisation_cost_usd=summarisation.cost_usd,
                        model_calls=totals.calls,
                        input_tokens=totals.input_tokens,
                        output_tokens=totals.output_tokens,
                        cost_usd=totals.cost_usd,
                        outcome="failure" if totals.error else "success",
                        error=totals.error,
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("failed to record channel run for %s", totals.channel_jid)

    async def _finalise(self, error: str | None) -> None:
        worked = [c for c in self.channels if c.messages]
        costs = [c.cost_usd for c in self.channels if c.cost_usd is not None]
        try:
            async with get_session() as session:
                row = await session.get(IngestRun, self.run_id)
                if row is None:  # pragma: no cover - defensive
                    return
                row.finished_at = datetime.now(UTC)
                row.duration_ms = int((time.monotonic() - self._started) * 1000)
                row.channels_total = len(self.channels)
                row.channels_with_work = len(worked)
                row.messages = self.messages
                row.threads_created = sum(c.threads_created for c in self.channels)
                row.threads_updated = sum(c.threads_updated for c in self.channels)
                row.threads_revived = sum(c.threads_revived for c in self.channels)
                row.chatter = sum(c.chatter for c in self.channels)
                row.unassigned = sum(c.unassigned for c in self.channels)
                row.model_calls = sum(c.calls for c in self.channels)
                row.input_tokens = sum(c.input_tokens for c in self.channels)
                row.output_tokens = sum(c.output_tokens for c in self.channels)
                row.cost_usd = sum(costs) if costs else None
                row.outcome = "failure" if error else "success"
                row.error = error
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("failed to finalise ingest run %s", self.run_id)

    def report(self) -> str | None:
        """The logs-channel message, or None when this run isn't worth
        reporting (see `shared.observability.report`)."""
        from shared.observability.report import render_ingest_report

        return render_ingest_report(self)


@asynccontextmanager
async def ingest_run(forced: bool = False) -> AsyncIterator[IngestRunRecorder]:
    """Wrap one sweep of the ingest tick. Always writes a run row (so a
    crashed run is visible); whether it gets *reported* to WhatsApp is
    `report()`'s decision."""
    async with get_session() as session:
        row = IngestRun(forced=forced)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        run_id = row.id

    recorder = IngestRunRecorder(run_id, forced)
    try:
        yield recorder
    except Exception as exc:
        await recorder._finalise(f"{type(exc).__name__}: {exc}")
        raise
    else:
        await recorder._finalise(None)
