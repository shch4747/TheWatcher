"""
The WA agent's main loop / scheduler.

Runs three periodic jobs:

  1. Outbound flush  — drain outputmessages.md, summarise per group, send via
                       gowa. Runs every FLUSH_INTERVAL_S.

  2. Reminder job    — build event/task/stale-project reminders from the wiki,
                       enqueue them for the next flush. Runs every
                       REMINDER_INTERVAL_S.

  3. Ingest check    — for each group buffer, if the batch trigger has fired
                       (N messages or age), run ingest_batch. This is normally
                       driven by the webhook, but the scheduler catches any
                       groups whose time-trigger fired between webhooks.

Usage:
    from scheduler import run_scheduler
    run_scheduler(pipeline, registry, sender)

Or start it from the CLI:
    python scheduler.py              # uses env vars for Lapis/gowa config
"""
from __future__ import annotations

import os
import time
import threading
from typing import Optional

from pipeline import WAPipeline
from group_context import GroupRegistry
from reminders import run_reminder_job, ReminderState
from webhook import GroupBuffers


FLUSH_INTERVAL_S = int(os.environ.get("WA_FLUSH_INTERVAL_S", "120"))    # 2 min
REMINDER_INTERVAL_S = int(os.environ.get("WA_REMINDER_INTERVAL_S", "3600"))  # 1 hr
INGEST_CHECK_S = int(os.environ.get("WA_INGEST_CHECK_S", "30"))         # 30 sec


def run_scheduler(
    pipeline: WAPipeline,
    registry: Optional[GroupRegistry] = None,
    buffers: Optional[GroupBuffers] = None,
    default_audience: str = "",
    once: bool = False,
) -> None:
    """Start the periodic scheduler. Blocks forever unless `once=True`
    (run one cycle and return — useful for tests and cron-driven setups)."""

    last_flush = 0.0
    last_reminder = 0.0
    # Reminder dedup state — persisted to wiki, survives restarts
    reminder_state = ReminderState(memory=pipeline.memory)

    def _tick():
        nonlocal last_flush, last_reminder
        now = time.time()

        # 1. Ingest check: flush any group buffers whose trigger has fired
        if buffers:
            for batch in buffers.ready_batches():
                pipeline.ingest_batch(batch)

        # 2. Outbound flush
        if now - last_flush >= FLUSH_INTERVAL_S:
            sent = pipeline.flush_notifications(registry=registry)
            if sent:
                print(f"  [scheduler] flushed {sent} outbound message(s)")
            last_flush = now

        # 3. Reminder job
        if now - last_reminder >= REMINDER_INTERVAL_S:
            audience = default_audience
            if not audience and registry:
                groups = registry.announce_groups()
                audience = groups[0] if groups else ""
            if audience:
                n = run_reminder_job(pipeline.memory, audience, state=reminder_state)
                if n:
                    print(f"  [scheduler] enqueued {n} reminder(s)")
            last_reminder = now

    if once:
        _tick()
        return

    print(f"[scheduler] running: flush every {FLUSH_INTERVAL_S}s, "
          f"reminders every {REMINDER_INTERVAL_S}s, "
          f"ingest check every {INGEST_CHECK_S}s")
    while True:
        try:
            _tick()
        except Exception as e:
            print(f"  [scheduler] error: {type(e).__name__}: {e}")
        time.sleep(INGEST_CHECK_S)


def start_scheduler_thread(
    pipeline: WAPipeline,
    registry: Optional[GroupRegistry] = None,
    buffers: Optional[GroupBuffers] = None,
    default_audience: str = "",
) -> threading.Thread:
    """Start the scheduler in a daemon thread (for use alongside the Flask
    webhook server)."""
    t = threading.Thread(
        target=run_scheduler,
        kwargs=dict(pipeline=pipeline, registry=registry,
                    buffers=buffers, default_audience=default_audience),
        daemon=True,
        name="wa-scheduler",
    )
    t.start()
    return t


# -- standalone entry point ---------------------------------------------------

if __name__ == "__main__":
    from lapis_client import HttpLapisClient
    from lapis_adapter import LapisAdapter
    from dispatcher import QueueDispatcher
    from gowa_client import make_sender

    client = HttpLapisClient()
    memory = LapisAdapter(client)
    dispatcher = QueueDispatcher(client)
    registry = GroupRegistry.from_wiki(client)
    sender = make_sender()

    pipeline = WAPipeline(
        memory=memory,
        dispatcher=dispatcher,
        allowlist=registry.allowlist(),
        sender=sender,
        noisy_jids=registry.noisy_groups(),
    )
    buffers = GroupBuffers(registry=registry)

    announce = registry.announce_groups()
    default_audience = announce[0] if announce else ""

    run_scheduler(pipeline, registry=registry, buffers=buffers,
                  default_audience=default_audience)
