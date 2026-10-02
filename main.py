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
    InboundMessage,
    list_channels,
    notify_logs,
    register_health_line,
    register_message_hook,
    register_reaction_hook,
)
from shared.inbox.interface import register_consumer
from shared.models.interface import client_for_model, default_decision_client, worker_model
from shared.observability.interface import ingest_run, setup_tracing
from shared.scheduler.interface import RunAfter, RunEvery, due_jobs, register, request_run, run_job
from shared.wiki.interface import default_vault_client

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

SCHEDULER_POLL_SECONDS = 30


class WatcherApplication:
    """Owns startup wiring: hooks, jobs, and the combined FastAPI + scheduler loop."""

    def setup(self) -> None:
        register_message_hook(self._chat_hook)
        register_reaction_hook(handle_reaction)
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
        worker = worker_model()
        decision = default_decision_client(worker)
        await handle_chat_message(message, default_vault_client(), decision, worker)

    async def _ingest_tick(self, force: bool = False) -> None:
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

    async def _ingest_now(self) -> None:
        await self._ingest_tick(force=True)

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
