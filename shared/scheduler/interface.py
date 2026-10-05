"""Scheduler interface (ADR-0004: its own module, ADR-0007: waits are rows,
not suspended runs). Jobs are registered in code, not a DSL; this module
gives every agent job registration, a run ledger, per-key locks so two
overlapping triggers on the same key never run concurrently, and
retry/backoff.

The default process-wide `Scheduler` sits behind these functions so
existing callers stay unchanged. Tests that need isolation construct
their own `Scheduler`.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TypeVar

from shared.db import Run
from shared.scheduler.scheduler import FailureHook, JobSpec, RunResult, Scheduler
from shared.scheduler.triggers import RunAfter, RunAt, RunEvery, RunNow, Trigger, is_due

ResultT = TypeVar("ResultT")

__all__ = [
    "Scheduler",
    "JobSpec",
    "FailureHook",
    "RunResult",
    "register",
    "unregister_all",
    "due_jobs",
    "run_job",
    "try_run_exclusive",
    "request_run",
    "registered_jobs",
    "job_spec",
    "run_after_event",
    "ledger_tail",
    "render_agent_status",
    "RunNow",
    "RunAt",
    "RunEvery",
    "RunAfter",
    "Trigger",
    "is_due",
]

_default = Scheduler()
# Test-compat aliases: the same dicts the default instance uses.
_registry = _default._registry
_requested = _default._requested


def register(
    name: str,
    handler,
    trigger: Trigger,
    lock_key: str | None = None,
    max_retries: int = 0,
    base_backoff_s: float = 0.01,
    on_failure: FailureHook | None = None,
) -> None:
    _default.register(
        name,
        handler,
        trigger,
        lock_key=lock_key,
        max_retries=max_retries,
        base_backoff_s=base_backoff_s,
        on_failure=on_failure,
    )


def unregister_all() -> None:
    _default.unregister_all()


def registered_jobs() -> list[str]:
    return _default.registered_jobs()


def job_spec(name: str) -> JobSpec:
    return _default.job_spec(name)


def request_run(name: str) -> None:
    _default.request_run(name)


async def due_jobs(now: datetime | None = None) -> list[str]:
    return await _default.due_jobs(now)


async def run_job(name: str) -> RunResult:
    return await _default.run_job(name)


async def try_run_exclusive(
    lock_key: str, handler: Callable[[], Awaitable[ResultT]]
) -> tuple[bool, ResultT | None]:
    return await _default.try_run_exclusive(lock_key, handler)


async def run_after_event(event: str) -> list[RunResult]:
    return await _default.run_after_event(event)


async def ledger_tail(job_name: str, limit: int = 20) -> list[Run]:
    return await _default.ledger_tail(job_name, limit)


def render_agent_status(agent: str, runs: list[Run]) -> str:
    return _default.render_agent_status(agent, runs)
