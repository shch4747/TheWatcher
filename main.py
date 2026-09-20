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
    run_batch,
    sunday_stale_nudge,
)
from shared.db import init_db
from shared.gateway.app import app as fastapi_app
from shared.gateway.interface import list_channels, notify_admins, register_message_hook
from shared.models.decision import default_decision_client
from shared.models.text import worker_model
from shared.scheduler.interface import RunAfter, RunEvery, due_jobs, register, run_job
from shared.wiki.interface import default_vault_client, slugify

logger = logging.getLogger(__name__)

# How often the tick loop checks for due jobs - independent of each
# job's own RunEvery interval below.
SCHEDULER_POLL_SECONDS = 30


async def _chat_hook(payload: dict, channel_jid: str) -> None:
    worker = worker_model()
    decision = default_decision_client(worker)
    await handle_chat_message(payload, channel_jid, default_vault_client(), decision, worker)


async def _ingest_tick(force: bool = False) -> None:
    vault = default_vault_client()
    worker = worker_model()
    decision = default_decision_client(worker)
    for channel in await list_channels():
        await run_batch(channel.jid, vault, decision, worker, force=force)


async def _ingest_now() -> None:
    """`/ingest`'s job (shared.gateway.interface.trigger_ingest): cuts
    whatever's unprocessed right now, ignoring BATCH_N/T/QUIET - unlike
    `ingest_tick`'s own scheduled runs, which always respect them."""
    await _ingest_tick(force=True)


async def _lifecycle_tick() -> None:
    vault = default_vault_client()
    for channel in await list_channels():
        channel_dir = slugify(channel.title or channel.jid)
        await check_and_mark_stale(vault, channel_dir)
        await archive_ended_threads(vault, channel_dir)


async def _sunday_nudge_tick() -> None:
    if datetime.now(UTC).weekday() == 6:  # Sunday
        await sunday_stale_nudge(default_vault_client())


async def _project_agent_tick() -> None:
    await run_project_agent_once(default_vault_client(), worker_model())


async def _notify_job_failure(job_name: str, error: str) -> None:
    """A scheduled job exhausted its retries - tell a Bot Admin over
    WhatsApp instead of leaving it only in `docker compose logs` and
    the run ledger (`/health` also shows the last run of each job, for
    checking proactively rather than waiting for this push)."""
    posted = await notify_admins(f"⚠️ *{job_name}* failed: {error}")
    if not posted:
        logger.warning("job %s failed and there's no coordis channel to notify: %s", job_name, error)


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
    setup_jobs()
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(server.serve(), scheduler_loop())


if __name__ == "__main__":
    asyncio.run(main())
