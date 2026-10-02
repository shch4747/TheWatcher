"""In-process job scheduler: registry, locks, ledger, retries."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from shared.db import Job, Run, aware_utc, get_session
from shared.observability.interface import scope
from shared.scheduler.triggers import RunAfter, Trigger, is_due

logger = logging.getLogger(__name__)

Handler = Callable[[], Awaitable[None]]
FailureHook = Callable[[str, str], Awaitable[None]]


@dataclass
class JobSpec:
    name: str
    handler: Handler
    trigger: Trigger
    lock_key: str | None = None
    max_retries: int = 0
    base_backoff_s: float = 0.01
    on_failure: FailureHook | None = None


@dataclass
class RunResult:
    job_name: str
    attempt: int
    outcome: str  # "success" | "failure"
    error: str | None = None


class Scheduler:
    """One registry + lock table. Tests can construct a second instance
    instead of sharing process-global state."""

    def __init__(self) -> None:
        self._registry: dict[str, JobSpec] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._requested: set[str] = set()

    def register(
        self,
        name: str,
        handler: Handler,
        trigger: Trigger,
        lock_key: str | None = None,
        max_retries: int = 0,
        base_backoff_s: float = 0.01,
        on_failure: FailureHook | None = None,
    ) -> None:
        self._registry[name] = JobSpec(
            name=name,
            handler=handler,
            trigger=trigger,
            lock_key=lock_key,
            max_retries=max_retries,
            base_backoff_s=base_backoff_s,
            on_failure=on_failure,
        )

    def unregister_all(self) -> None:
        self._registry.clear()
        self._requested.clear()

    def registered_jobs(self) -> list[str]:
        return sorted(self._registry)

    def job_spec(self, name: str) -> JobSpec:
        return self._registry[name]

    def request_run(self, name: str) -> None:
        if name not in self._registry:
            raise KeyError(name)
        self._requested.add(name)

    def _lock_for(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def _last_finished_at(self, job_name: str) -> datetime | None:
        async with get_session() as session:
            row = await session.scalar(
                select(Run)
                .where(Run.job_name == job_name, Run.outcome == "success")
                .order_by(Run.finished_at.desc())
                .limit(1)
            )
        return aware_utc(row.finished_at) if row else None

    async def due_jobs(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(UTC)
        due = [name for name in sorted(self._requested) if name in self._registry]
        self._requested.clear()
        for spec in self._registry.values():
            if spec.name in due:
                continue
            if isinstance(spec.trigger, RunAfter):
                continue
            last = await self._last_finished_at(spec.name)
            if is_due(spec.trigger, last, now):
                due.append(spec.name)
        return due

    async def run_job(self, name: str) -> RunResult:
        spec = self._registry[name]
        lock = self._lock_for(spec.lock_key) if spec.lock_key else None
        if lock is not None:
            async with lock:
                return await self._run_with_retries(spec)
        return await self._run_with_retries(spec)

    async def _run_with_retries(self, spec: JobSpec) -> RunResult:
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
                with scope(job_name=spec.name):
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
                if spec.on_failure is not None:
                    try:
                        await spec.on_failure(spec.name, last_error)
                    except Exception:  # noqa: BLE001 - a broken notifier must never hide the real failure
                        logger.exception("on_failure hook for job %s raised", spec.name)
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

    async def run_after_event(self, event: str) -> list[RunResult]:
        results = []
        for spec in self._registry.values():
            if isinstance(spec.trigger, RunAfter) and spec.trigger.event == event:
                results.append(await self.run_job(spec.name))
        return results

    async def ledger_tail(self, job_name: str, limit: int = 20) -> list[Run]:
        async with get_session() as session:
            rows = await session.scalars(
                select(Run).where(Run.job_name == job_name).order_by(Run.started_at.desc()).limit(limit)
            )
        return list(rows)

    def render_agent_status(self, agent: str, runs: list[Run]) -> str:
        lines = [f"# {agent}", "", "## Run ledger (most recent first)", ""]
        for run in runs:
            finished = run.finished_at.isoformat() if run.finished_at else "running"
            lines.append(f"- {run.started_at.isoformat()} -> {finished} [{run.outcome or 'running'}]")
            if run.error:
                lines.append(f"  error: {run.error}")
        return "\n".join(lines) + "\n"
