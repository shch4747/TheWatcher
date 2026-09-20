"""Scheduler interface (ADR-0004: its own module, ADR-0007: waits are rows,
not suspended runs). Jobs are registered in code, not a DSL; this module
gives every agent job registration, a run ledger, per-key locks so two
overlapping triggers on the same key never run concurrently, and
retry/backoff. Regenerating `meta/agents/<agent>.md` from the ledger is
the wiki layer's job (Phase 1's derived-regeneration ticket) once a Lapis
client exists - `render_agent_status` here only produces the markdown.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from shared.db import Job, Run, aware_utc, get_session
from shared.scheduler.triggers import RunAfter, RunAt, RunEvery, RunNow, Trigger, is_due

__all__ = [
    "JobSpec",
    "RunResult",
    "register",
    "unregister_all",
    "due_jobs",
    "run_job",
    "run_after_event",
    "ledger_tail",
    "render_agent_status",
    "RunNow",
    "RunAt",
    "RunEvery",
    "RunAfter",
    "Trigger",
]

logger = logging.getLogger(__name__)

Handler = Callable[[], Awaitable[None]]


@dataclass
class JobSpec:
    name: str
    handler: Handler
    trigger: Trigger
    lock_key: str | None = None
    max_retries: int = 0
    base_backoff_s: float = 0.01


@dataclass
class RunResult:
    job_name: str
    attempt: int
    outcome: str  # "success" | "failure"
    error: str | None = None


_registry: dict[str, JobSpec] = {}
_locks: dict[str, asyncio.Lock] = {}


def register(
    name: str,
    handler: Handler,
    trigger: Trigger,
    lock_key: str | None = None,
    max_retries: int = 0,
    base_backoff_s: float = 0.01,
) -> None:
    _registry[name] = JobSpec(
        name=name,
        handler=handler,
        trigger=trigger,
        lock_key=lock_key,
        max_retries=max_retries,
        base_backoff_s=base_backoff_s,
    )


def unregister_all() -> None:
    """Test-only: clear the registry between test cases."""
    _registry.clear()


def _lock_for(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


async def _last_finished_at(job_name: str) -> datetime | None:
    async with get_session() as session:
        row = await session.scalar(
            select(Run)
            .where(Run.job_name == job_name, Run.outcome == "success")
            .order_by(Run.finished_at.desc())
            .limit(1)
        )
    return aware_utc(row.finished_at) if row else None


async def due_jobs(now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC)
    due = []
    for spec in _registry.values():
        if isinstance(spec.trigger, RunAfter):
            continue
        last = await _last_finished_at(spec.name)
        if is_due(spec.trigger, last, now):
            due.append(spec.name)
    return due


async def run_job(name: str) -> RunResult:
    spec = _registry[name]
    lock = _lock_for(spec.lock_key) if spec.lock_key else None

    if lock is not None:
        async with lock:
            return await _run_with_retries(spec)
    return await _run_with_retries(spec)


async def _run_with_retries(spec: JobSpec) -> RunResult:
    async with get_session() as session:
        existing = await session.get(Job, spec.name)
        if existing is None:
            session.add(Job(name=spec.name, lock_key=spec.lock_key, max_retries=spec.max_retries))
            await session.commit()

    last_error: str | None = None
    for attempt in range(1, spec.max_retries + 2):
        run_row = Run(job_name=spec.name, attempt=attempt)
        async with get_session() as session:
            session.add(run_row)
            await session.commit()
            await session.refresh(run_row)

        try:
            await spec.handler()
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed silently
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("job %s attempt %d failed: %s", spec.name, attempt, last_error)
            async with get_session() as session:
                db_run = await session.get(Run, run_row.id)
                assert db_run is not None
                db_run.outcome = "failure"
                db_run.error = last_error
                db_run.finished_at = datetime.now(UTC)
                await session.commit()
            if attempt <= spec.max_retries:
                await asyncio.sleep(spec.base_backoff_s * (2 ** (attempt - 1)))
                continue
            return RunResult(job_name=spec.name, attempt=attempt, outcome="failure", error=last_error)
        else:
            async with get_session() as session:
                db_run = await session.get(Run, run_row.id)
                assert db_run is not None
                db_run.outcome = "success"
                db_run.finished_at = datetime.now(UTC)
                await session.commit()
            return RunResult(job_name=spec.name, attempt=attempt, outcome="success")

    raise AssertionError("unreachable: loop always returns or the last iteration returns")


async def run_after_event(event: str) -> list[RunResult]:
    """Fire every job whose trigger is `RunAfter(event)`."""
    results = []
    for spec in _registry.values():
        if isinstance(spec.trigger, RunAfter) and spec.trigger.event == event:
            results.append(await run_job(spec.name))
    return results


async def ledger_tail(job_name: str, limit: int = 20) -> list[Run]:
    async with get_session() as session:
        rows = await session.scalars(
            select(Run).where(Run.job_name == job_name).order_by(Run.started_at.desc()).limit(limit)
        )
    return list(rows)


def render_agent_status(agent: str, runs: list[Run]) -> str:
    """`meta/agents/<agent>.md` body (Wiki Format: purpose/inputs/outputs/
    schedule + last 20 ledger rows). Persisting it is the wiki layer's job."""
    lines = [f"# {agent}", "", "## Run ledger (most recent first)", ""]
    for run in runs:
        finished = run.finished_at.isoformat() if run.finished_at else "running"
        lines.append(f"- {run.started_at.isoformat()} -> {finished} [{run.outcome or 'running'}]")
        if run.error:
            lines.append(f"  error: {run.error}")
    return "\n".join(lines) + "\n"
