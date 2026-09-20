"""Phase 3 acceptance criteria (docs/Plan - Watcher v1.md):
overlapping triggers on the same key run serially; a failing job retries
and its ledger shows it; agent status renders from the ledger."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from shared.scheduler import interface as scheduler
from shared.scheduler.triggers import RunAfter, RunAt, RunEvery, RunNow, is_due


@pytest.fixture(autouse=True)
def _clear_registry():
    scheduler.unregister_all()
    yield
    scheduler.unregister_all()


async def test_two_overlapping_triggers_same_key_run_serially():
    concurrent = 0
    max_concurrent = 0

    async def handler() -> None:
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.02)
        concurrent -= 1

    scheduler.register("job-a", handler, RunNow(), lock_key="shared-key")
    scheduler.register("job-b", handler, RunNow(), lock_key="shared-key")

    await asyncio.gather(scheduler.run_job("job-a"), scheduler.run_job("job-b"))

    assert max_concurrent == 1


async def test_failing_job_retries_and_ledger_shows_it():
    attempts = 0

    async def flaky() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError(f"boom on attempt {attempts}")

    scheduler.register("flaky-job", flaky, RunNow(), max_retries=3, base_backoff_s=0.001)
    result = await scheduler.run_job("flaky-job")

    assert result.outcome == "success"
    assert attempts == 3

    ledger = await scheduler.ledger_tail("flaky-job")
    assert len(ledger) == 3
    outcomes = sorted(r.outcome for r in ledger)
    assert outcomes == ["failure", "failure", "success"]


async def test_job_exhausting_retries_ends_in_failure():
    async def always_fails() -> None:
        raise RuntimeError("nope")

    scheduler.register("doomed-job", always_fails, RunNow(), max_retries=1, base_backoff_s=0.001)
    result = await scheduler.run_job("doomed-job")

    assert result.outcome == "failure"
    assert "nope" in (result.error or "")

    ledger = await scheduler.ledger_tail("doomed-job")
    assert len(ledger) == 2
    assert all(r.outcome == "failure" for r in ledger)


async def test_run_after_event_fires_only_matching_jobs():
    fired = []

    async def on_backfill() -> None:
        fired.append("backfill")

    async def on_other() -> None:
        fired.append("other")

    scheduler.register("j1", on_backfill, RunAfter("backfill_done"))
    scheduler.register("j2", on_other, RunAfter("something_else"))

    results = await scheduler.run_after_event("backfill_done")

    assert fired == ["backfill"]
    assert [r.job_name for r in results] == ["j1"]


async def test_due_jobs_reflects_trigger_state():
    async def noop() -> None:
        pass

    scheduler.register("run-once", noop, RunNow())
    assert await scheduler.due_jobs() == ["run-once"]

    await scheduler.run_job("run-once")
    assert await scheduler.due_jobs() == []


def test_is_due_run_at_and_run_every():
    now = datetime.now(UTC)
    assert is_due(RunAt(now - timedelta(seconds=1)), last_finished_at=None, now=now) is True
    assert is_due(RunAt(now + timedelta(hours=1)), last_finished_at=None, now=now) is False

    every = RunEvery(timedelta(minutes=5))
    assert is_due(every, last_finished_at=None, now=now) is True
    assert is_due(every, last_finished_at=now - timedelta(minutes=1), now=now) is False
    assert is_due(every, last_finished_at=now - timedelta(minutes=10), now=now) is True


def test_render_agent_status_includes_ledger_rows():
    from shared.db import Run

    runs = [
        Run(
            id=1,
            job_name="lint",
            started_at=datetime(2026, 9, 19, tzinfo=UTC),
            finished_at=datetime(2026, 9, 19, 0, 1, tzinfo=UTC),
            outcome="success",
        )
    ]
    text = scheduler.render_agent_status("lint-agent", runs)
    assert "# lint-agent" in text
    assert "success" in text
