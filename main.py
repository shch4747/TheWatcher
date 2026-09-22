"""Composition root: wires the agents into the Scheduler and the Chat
Agent into the Gateway's message hook, then runs the FastAPI app and a
scheduler tick loop together. This is the one place allowed to import
across `shared/*` and `agents/*` - both of those stay agent/composition-
agnostic per `AGENTS.md`.

Run with: `uv run python main.py` (this is what the Dockerfile runs).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import uvicorn
from agents.project_agent.interface import run_project_agent_once
from agents.wa_agent.interface import (
    archive_ended_threads,
    check_and_mark_stale,
    handle_chat_message,
    regenerate_channel_threads_index,
    run_batch,
    sunday_stale_nudge,
)
from shared.config import settings
from shared.db import init_db
from shared.gateway.app import app as fastapi_app
from shared.gateway.interface import list_channels, notify_logs, register_message_hook
from shared.models.decision import default_decision_client
from shared.models.text import client_for_model, worker_model
from shared.observability.interface import ingest_run, setup_tracing
from shared.scheduler.interface import RunAfter, RunEvery, due_jobs, register, run_job
from shared.wiki.interface import default_vault_client, slugify

logger = logging.getLogger(__name__)

# Without this only uvicorn's own loggers are configured: every
# logger.info in the app is dropped, and warnings reach stderr only via
# logging's lastResort handler, unformatted and untimestamped. An
# observability build that can't see its own logs is not much of one.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

# How often the tick loop checks for due jobs - independent of each
# job's own RunEvery interval below.
SCHEDULER_POLL_SECONDS = 30


async def _chat_hook(payload: dict, channel_jid: str) -> None:
    worker = worker_model()
    decision = default_decision_client(worker)
    await handle_chat_message(payload, channel_jid, default_vault_client(), decision, worker)


async def _ingest_tick(force: bool = False) -> None:
    """Structured ingestion (ADR-0012) - Worker model only, no Decision
    Model. INGEST_MODEL_NAME swaps the model used for the assignment
    and thread-update calls without touching the Worker elsewhere.

    Wrapped in `ingest_run` (ADR-0013), which times each channel and
    rolls its model calls up by phase into `obs_ingest_channel_runs`.
    The report is *sent* from here rather than from inside
    observability: that package must not import the gateway, or the
    scheduler->observability->gateway->scheduler cycle closes."""
    vault = default_vault_client()
    worker = worker_model()
    assign_client = client_for_model(settings.ingest_model_name) if settings.ingest_model_name else None
    async with ingest_run(forced=force) as run:
        for channel in await list_channels():
            async with run.channel(channel):
                run.record(
                    await run_batch(
                        channel.jid, vault, worker, force=force, assign_client=assign_client
                    )
                )
    report = run.report()
    if report:
        await notify_logs(report)


async def _ingest_now() -> None:
    """`/ingest`'s job (shared.gateway.interface.trigger_ingest): cuts
    whatever's unprocessed right now, ignoring BATCH_N/T/QUIET - unlike
    `ingest_tick`'s own scheduled runs, which always respect them."""
    await _ingest_tick(force=True)


async def _lifecycle_tick() -> None:
    vault = default_vault_client()
    for channel in await list_channels():
        channel_dir = slugify(channel.title or channel.jid)
        stale = await check_and_mark_stale(vault, channel_dir)
        archived = await archive_ended_threads(vault, channel_dir)
        if stale or archived:
            await regenerate_channel_threads_index(vault, channel, channel_dir)


async def _sunday_nudge_tick() -> None:
    if datetime.now(UTC).weekday() == 6:  # Sunday
        await sunday_stale_nudge(default_vault_client())


async def _project_agent_tick() -> None:
    await run_project_agent_once(default_vault_client(), worker_model())


async def _notify_job_failure(job_name: str, error: str) -> None:
    """A scheduled job exhausted its retries - tell a Bot Admin over
    WhatsApp instead of leaving it only in `docker compose logs` and
    the run ledger (`/health` also shows the last run of each job, for
    checking proactively rather than waiting for this push). Always the
    logs channel (`/setup logs`), never coordis - coordis is reserved
    for Proposals."""
    posted = await notify_logs(f"⚠️ *{job_name}* failed: {error}")
    if not posted:
        logger.warning("job %s failed and there's no logs channel to notify: %s", job_name, error)


def setup_jobs() -> None:
    register_message_hook(_chat_hook)
    register(
        "ingest_tick",
        _ingest_tick,
        RunEvery(timedelta(minutes=2)),
        lock_key="ingest_tick",
        max_retries=1,
        on_failure=_notify_job_failure,
    )
    register(
        "ingest_now",
        _ingest_now,
        # RunAfter is never auto-fired by due_jobs() - only /ingest calls run_job("ingest_now") directly.
        RunAfter("manual-ingest"),
        lock_key="ingest_tick",  # shares ingest_tick's lock so the two can't run concurrently
        max_retries=1,
        on_failure=_notify_job_failure,
    )
    register(
        "lifecycle_tick",
        _lifecycle_tick,
        RunEvery(timedelta(hours=24)),
        lock_key="lifecycle_tick",
        max_retries=1,
        on_failure=_notify_job_failure,
    )
    register(
        "sunday_nudge_tick",
        _sunday_nudge_tick,
        RunEvery(timedelta(hours=24)),
        lock_key="sunday_nudge_tick",
        max_retries=1,
        on_failure=_notify_job_failure,
    )
    register(
        "project_agent_tick",
        _project_agent_tick,
        RunEvery(timedelta(minutes=2)),
        lock_key="project_agent_tick",
        max_retries=1,
        on_failure=_notify_job_failure,
    )


async def scheduler_loop(poll_seconds: float = SCHEDULER_POLL_SECONDS) -> None:
    while True:
        for name in await due_jobs():
            await run_job(name)
        await asyncio.sleep(poll_seconds)


async def main() -> None:
    # init_db() also runs in the FastAPI app's own lifespan, but that
    # fires as part of server.serve() below - awaiting it here first
    # avoids a race where the scheduler loop's first tick queries the
    # DB before the app has had a chance to create the tables.
    await init_db()
    setup_tracing()
    setup_jobs()
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(server.serve(), scheduler_loop())


if __name__ == "__main__":
    asyncio.run(main())
