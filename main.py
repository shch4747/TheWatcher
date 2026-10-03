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
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import uvicorn
from agents.project_agent.interface import run_project_agent_once
from agents.wa_agent.interface import (
    IngestionPipeline,
    expire_stale_proposals,
    handle_chat_message,
    handle_reaction,
    pending_proposals_line,
    run_batch,
    run_lifecycle_for_channel,
    sunday_stale_nudge,
)
from shared.config import settings
from shared.db import init_db
from shared.gateway.app import app as fastapi_app
from shared.gateway.interface import (
    ChannelInfo,
    InboundMessage,
    list_channels,
    notify_logs,
    register_channel_watched_hook,
    register_health_line,
    register_message_hook,
    register_reaction_hook,
)
from shared.inbox.interface import register_consumer
from shared.models.interface import (
    assignment_model,
    openrouter_jev_client,
    wa_worker_model,
    worker_model,
)
from shared.observability.interface import ingest_run, setup_tracing
from shared.scheduler.interface import RunAfter, RunEvery, due_jobs, register, request_run, run_job
from shared.wiki.interface import default_vault_client

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

SCHEDULER_POLL_SECONDS = 30


BACKFILL_JOB = "backfill_ingest"


class WatchedChannelQueue:
    """Channels `/setup` has just imported history for, waiting for their
    first full ingest. In memory on purpose: if the process restarts first,
    the imported messages are still in the buffer and the regular
    `ingest_tick` picks them up."""

    def __init__(self) -> None:
        self._jids: dict[str, None] = {}

    def add(self, jid: str) -> None:
        self._jids[jid] = None

    def pending(self) -> list[str]:
        return list(self._jids)

    def done(self, jid: str) -> None:
        self._jids.pop(jid, None)


class WatcherApplication:
    """Owns startup wiring: hooks, jobs, and the combined FastAPI + scheduler loop."""

    def __init__(self) -> None:
        self.watched = WatchedChannelQueue()

    def setup(self) -> None:
        register_message_hook(self._chat_hook)
        register_reaction_hook(handle_reaction)
        register_channel_watched_hook(self._channel_watched_hook)
        register_health_line(pending_proposals_line)
        register(
            "ingest_tick",
            self._ingest_tick,
            RunEvery(timedelta(minutes=2)),
            lock_key="ingest_tick",
            max_retries=1,
            on_failure=self._notify_job_failure,
        )
        register(
            "ingest_now",
            self._ingest_now,
            RunAfter("manual-ingest"),
            lock_key="ingest_tick",
            max_retries=1,
            on_failure=self._notify_job_failure,
        )
        register(
            BACKFILL_JOB,
            self._backfill_ingest,
            RunAfter("channel-watched"),
            lock_key="ingest_tick",
            max_retries=1,
            on_failure=self._notify_job_failure,
        )
        register(
            "lifecycle_tick",
            self._lifecycle_tick,
            RunEvery(timedelta(hours=24)),
            lock_key="lifecycle_tick",
            max_retries=1,
            on_failure=self._notify_job_failure,
        )
        register(
            "sunday_nudge_tick",
            self._sunday_nudge_tick,
            RunEvery(timedelta(hours=24)),
            lock_key="sunday_nudge_tick",
            max_retries=1,
            on_failure=self._notify_job_failure,
        )
        register(
            "project_agent_tick",
            self._project_agent_tick,
            RunEvery(timedelta(minutes=2)),
            lock_key="project_agent_tick",
            max_retries=1,
            on_failure=self._notify_job_failure,
        )
        register(
            "expire_proposals_tick",
            self._expire_proposals_tick,
            RunEvery(timedelta(hours=1)),
            lock_key="expire_proposals_tick",
            max_retries=1,
            on_failure=self._notify_job_failure,
        )
        register_consumer("project_agent", lambda: request_run("project_agent_tick"))

    async def _chat_hook(self, message: InboundMessage) -> None:
        await handle_chat_message(message, wa_worker_model(), default_vault_client())

    async def _channel_watched_hook(self, channel: ChannelInfo) -> None:
        self.watched.add(channel.jid)
        request_run(BACKFILL_JOB)

    async def _ingest_tick(self, force: bool = False) -> None:
        await self._ingest(await list_channels(), force=force)

    async def _ingest_now(self) -> None:
        await self._ingest_tick(force=True)

    async def _backfill_ingest(self) -> None:
        """First ingest of channels `/setup` just seeded with history:
        everything pending, however many batches that takes."""
        waiting = set(self.watched.pending())
        channels = [c for c in await list_channels() if c.jid in waiting]
        await self._ingest(channels, force=True, drain=True, done=self.watched.done)
        for jid in waiting - {c.jid for c in channels}:
            self.watched.done(jid)  # unwatched again before its turn

    async def _ingest(
        self,
        channels: list[ChannelInfo],
        force: bool = False,
        drain: bool = False,
        done: Callable[[str], None] | None = None,
    ) -> None:
        vault = default_vault_client()
        worker = wa_worker_model()
        assign_client = assignment_model()
        decider = openrouter_jev_client() if settings.assignment_mode == "jev" else None
        pipeline = IngestionPipeline(vault, worker, assign_client, decider)
        async with ingest_run(forced=force) as run:
            for channel in channels:
                async with run.channel(channel):
                    if drain:
                        run.record(await pipeline.run_drained(channel.jid))
                    else:
                        run.record(
                            await run_batch(
                                channel.jid,
                                vault,
                                worker,
                                force=force,
                                assign_client=assign_client,
                                decider=decider,
                            )
                        )
                if done is not None:
                    done(channel.jid)
        report = run.report()
        if report:
            await notify_logs(report)

    async def _lifecycle_tick(self) -> None:
        vault = default_vault_client()
        for channel in await list_channels():
            await run_lifecycle_for_channel(vault, channel)

    async def _sunday_nudge_tick(self) -> None:
        if datetime.now(UTC).weekday() == 6:
            await sunday_stale_nudge(default_vault_client())

    async def _project_agent_tick(self) -> None:
        await run_project_agent_once(default_vault_client(), worker_model())

    async def _notify_job_failure(self, job_name: str, error: str) -> None:
        posted = await notify_logs(f"⚠️ *{job_name}* failed: {error}")
        if not posted:
            logger.warning("job %s failed and there's no logs channel to notify: %s", job_name, error)

    async def _expire_proposals_tick(self) -> None:
        await expire_stale_proposals()

    async def scheduler_loop(self, poll_seconds: float = SCHEDULER_POLL_SECONDS) -> None:
        while True:
            for name in await due_jobs():
                await run_job(name)
            await asyncio.sleep(poll_seconds)

    async def run(self) -> None:
        await init_db()
        setup_tracing()
        self.setup()
        config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=8000, log_level="info")
        server = uvicorn.Server(config)
        await asyncio.gather(server.serve(), self.scheduler_loop())


def setup_jobs() -> None:
    """Compatibility wrapper used by tests and the historic entrypoint."""
    WatcherApplication().setup()


async def scheduler_loop(poll_seconds: float = SCHEDULER_POLL_SECONDS) -> None:
    await WatcherApplication().scheduler_loop(poll_seconds)


async def main() -> None:
    await WatcherApplication().run()


if __name__ == "__main__":
    asyncio.run(main())
