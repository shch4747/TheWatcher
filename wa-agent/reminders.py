"""
The reminder / task-chasing job.

Decisions baked in:
  - GitHub is linked to the WIKI, not to the WA agent. So this job reads
    everything from the wiki (event/project pages already carry reminder
    info, whether auto-derived from GitHub or entered manually). The WA
    agent never talks to GitHub directly.
  - Reminders "either present in the event/project wiki or manually made" —
    so this turns wiki reads into notification intents; it does not invent
    schedules.

`build_reminders` is pure given the memory reads, so it's easy to test.
The pipeline enqueues the intents; the outbound gateway sends them.
"""
from __future__ import annotations

import uuid

from schemas import NotificationIntent, Urgency
from memory_interface import WAMemory


def build_reminders(
    memory: WAMemory,
    default_audience: str,
    within_days: int = 3,
    stale_days: int = 7,
) -> list[NotificationIntent]:
    intents: list[NotificationIntent] = []

    for ev in memory.get_upcoming_events(within_days):
        audience = ev.get("group") or default_audience
        when = ev.get("date") or ev.get("when") or "soon"
        intents.append(_intent(
            "events", audience,
            f"Reminder: {ev.get('title', 'event')} — {when}"
            + (f" @ {ev['location']}" if ev.get("location") else ""),
            Urgency.NORMAL, ev.get("id"),
        ))

    pending = memory.get_pending_tasks()
    if pending:
        lines = ", ".join(t.get("title", "task") for t in pending[:5])
        more = f" (+{len(pending) - 5} more)" if len(pending) > 5 else ""
        intents.append(_intent(
            "tasks", default_audience,
            f"{len(pending)} open task(s): {lines}{more}", Urgency.NORMAL,
        ))

    for proj in memory.get_projects_needing_nudge(stale_days):
        audience = proj.get("group") or default_audience
        intents.append(_intent(
            "project", audience,
            f"Nudge: '{proj.get('title', proj.get('id', 'project'))}' has had no "
            f"update in {proj.get('days_stale', stale_days)}+ days. Still alive?",
            Urgency.LOW, proj.get("id"),
        ))

    return intents


def _intent(agent, audience, payload, urgency, ref=None) -> NotificationIntent:
    return NotificationIntent(
        id=str(uuid.uuid4())[:8], source_agent=agent, audience=audience,
        payload=payload, urgency=urgency, source_ids=[ref] if ref else [],
    )


def run_reminder_job(memory: WAMemory, default_audience: str, **kw) -> int:
    """Build reminders and enqueue them for the outbound gateway to send."""
    intents = build_reminders(memory, default_audience, **kw)
    for i in intents:
        memory.enqueue_notification(i)
    return len(intents)
