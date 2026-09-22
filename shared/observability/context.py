"""Ambient attribution for model calls and vault writes (ADR-0013).

`log_model_call` sits several frames below the code that knows *why* a
call is happening - which channel's batch, which phase of it. Threading
that down through every signature would touch a dozen call sites in
three packages, so it rides in a `ContextVar` instead: callers open a
`scope()` at the boundaries that already exist (a job, a batch, a
phase) and the recording functions read `current()`.

`asyncio` gives each task a copy of the context, so concurrent batches
never see each other's scope - that property is what makes this safe,
and `tests/test_observability_context.py` pins it.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace

# Phases a model call can belong to. classification/summarisation are
# the two ingestion calls (ADR-0012); the rest keep non-ingest calls
# from being lumped in with them on a dashboard.
CLASSIFICATION = "classification"
SUMMARISATION = "summarisation"
DECISION = "decision"
CHAT = "chat"
PROJECT_AGENT = "project_agent"


@dataclass(frozen=True)
class CallContext:
    ingest_run_id: int | None = None
    channel_jid: str | None = None
    phase: str | None = None
    job_name: str | None = None


_EMPTY = CallContext()
_ctx: ContextVar[CallContext] = ContextVar("watcher_obs_ctx", default=_EMPTY)


def current() -> CallContext:
    return _ctx.get()


@contextmanager
def scope(
    *,
    ingest_run_id: int | None = None,
    channel_jid: str | None = None,
    phase: str | None = None,
    job_name: str | None = None,
) -> Iterator[CallContext]:
    """Merge the given fields onto the current context for the duration
    of the block. Fields left as None are inherited, not cleared - an
    inner `scope(phase=...)` keeps the outer scope's channel."""
    base = _ctx.get()
    merged = replace(
        base,
        ingest_run_id=base.ingest_run_id if ingest_run_id is None else ingest_run_id,
        channel_jid=base.channel_jid if channel_jid is None else channel_jid,
        phase=base.phase if phase is None else phase,
        job_name=base.job_name if job_name is None else job_name,
    )
    token = _ctx.set(merged)
    try:
        yield merged
    finally:
        _ctx.reset(token)
