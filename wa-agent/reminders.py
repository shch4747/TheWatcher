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

Dedup: the `ReminderState` tracks which reminders have been sent (by
item ID + date) so the same reminder isn't spammed every hour. A reminder
for a given item is sent at most once per "window bracket" (e.g. once
when 3 days out, once when 1 day out, once on the day of).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from schemas import NotificationIntent, Urgency
from memory_interface import WAMemory


# -- reminder dedup state ----------------------------------------------------

# Window brackets: a reminder for the same item is re-sent only when it
# crosses into a closer bracket. E.g. an event 3 days out gets a reminder;
# the next one fires only when it's <=1 day out (or day-of).
_WINDOW_BRACKETS = [3, 1, 0]    # days out


def _bracket_for_days(days_until: int) -> int:
    """Return the tightest bracket an item falls into, or -1 if outside all.
    E.g. days_until=1 -> bracket 1, days_until=2 -> bracket 3."""
    for b in sorted(_WINDOW_BRACKETS):
        if days_until <= b:
            return b
    return -1


class ReminderState:
    """Tracks which reminders have been sent to avoid hourly spam.

    State is a dict of item_id -> {"last_bracket": int, "last_date": str}.
    Persisted via WAMemory's state mechanism (a JSON file in the wiki)."""

    STATE_KEY = "reminder_dedup"

    def __init__(self, memory: Optional[WAMemory] = None):
        self._sent: dict[str, dict] = {}
        self._memory = memory
        self._load()

    def _state_path(self) -> str:
        from lapis_adapter import LAYOUT
        return f"{LAYOUT['state']}/reminder_state.json"

    def _load(self) -> None:
        if not self._memory:
            return
        # Try to read from Lapis state
        try:
            raw = self._memory.c.read_file(self._state_path()) if hasattr(self._memory, 'c') else None
            if raw:
                self._sent = json.loads(raw)
        except Exception:
            self._sent = {}

    def _save(self) -> None:
        if not self._memory or not hasattr(self._memory, 'c'):
            return
        try:
            # Prune entries older than 30 days
            now = datetime.now(timezone.utc).date().isoformat()
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
            self._sent = {k: v for k, v in self._sent.items()
                          if v.get("last_date", "") >= cutoff}
            self._memory.c.write_file(self._state_path(), json.dumps(self._sent))
        except Exception:
            pass

    def should_send(self, item_id: str, days_until: int = 0) -> bool:
        """Return True if this reminder should be sent (hasn't been sent
        at this bracket yet)."""
        if not item_id:
            return True  # no ID = no dedup possible
        bracket = _bracket_for_days(days_until)
        prev = self._sent.get(item_id)
        if not prev:
            return True
        return prev.get("last_bracket", -1) > bracket  # closer bracket = lower number

    def mark_sent(self, item_id: str, days_until: int = 0) -> None:
        if not item_id:
            return
        self._sent[item_id] = {
            "last_bracket": _bracket_for_days(days_until),
            "last_date": datetime.now(timezone.utc).date().isoformat(),
        }

    def save(self) -> None:
        self._save()

    def should_send_task_digest(self) -> bool:
        """Task digest: send at most once per day."""
        prev = self._sent.get("__task_digest__")
        if not prev:
            return True
        return prev.get("last_date", "") < datetime.now(timezone.utc).date().isoformat()

    def mark_task_digest_sent(self) -> None:
        self._sent["__task_digest__"] = {
            "last_bracket": 0,
            "last_date": datetime.now(timezone.utc).date().isoformat(),
        }

    def should_send_nudge(self, item_id: str) -> bool:
        """Project nudges: at most once per day per project."""
        prev = self._sent.get(f"nudge:{item_id}")
        if not prev:
            return True
        return prev.get("last_date", "") < datetime.now(timezone.utc).date().isoformat()

    def mark_nudge_sent(self, item_id: str) -> None:
        self._sent[f"nudge:{item_id}"] = {
            "last_bracket": 0,
            "last_date": datetime.now(timezone.utc).date().isoformat(),
        }


# -- reminder builder -------------------------------------------------------

def _days_until(date_str: str) -> int:
    """Days from now until the given ISO date string."""
    try:
        d = datetime.fromisoformat(date_str).date()
        return (d - datetime.now(timezone.utc).date()).days
    except (ValueError, TypeError):
        return 0


def build_reminders(
    memory: WAMemory,
    default_audience: str,
    within_days: int = 3,
    stale_days: int = 7,
    state: Optional[ReminderState] = None,
) -> list[NotificationIntent]:
    intents: list[NotificationIntent] = []

    for ev in memory.get_upcoming_events(within_days):
        item_id = ev.get("id", "")
        when = ev.get("date") or ev.get("when") or "soon"
        days = _days_until(when) if when != "soon" else 0
        # Dedup: skip if already sent at this bracket
        if state and not state.should_send(item_id, days):
            continue
        audience = ev.get("group") or default_audience
        intents.append(_intent(
            "events", audience,
            f"Reminder: {ev.get('title', 'event')} — {when}"
            + (f" @ {ev['location']}" if ev.get("location") else ""),
            Urgency.NORMAL, item_id,
        ))
        if state:
            state.mark_sent(item_id, days)

    pending = memory.get_pending_tasks()
    if pending:
        # Dedup: task digest at most once per day
        if not state or state.should_send_task_digest():
            lines = ", ".join(t.get("title", "task") for t in pending[:5])
            more = f" (+{len(pending) - 5} more)" if len(pending) > 5 else ""
            intents.append(_intent(
                "tasks", default_audience,
                f"{len(pending)} open task(s): {lines}{more}", Urgency.NORMAL,
            ))
            if state:
                state.mark_task_digest_sent()

    for proj in memory.get_projects_needing_nudge(stale_days):
        item_id = proj.get("id", "")
        # Dedup: at most one nudge per project per day
        if state and not state.should_send_nudge(item_id):
            continue
        audience = proj.get("group") or default_audience
        intents.append(_intent(
            "project", audience,
            f"Nudge: '{proj.get('title', proj.get('id', 'project'))}' has had no "
            f"update in {proj.get('days_stale', stale_days)}+ days. Still alive?",
            Urgency.LOW, item_id,
        ))
        if state:
            state.mark_nudge_sent(item_id)

    return intents


def _intent(agent, audience, payload, urgency, ref=None) -> NotificationIntent:
    return NotificationIntent(
        id=str(uuid.uuid4())[:8], source_agent=agent, audience=audience,
        payload=payload, urgency=urgency, source_ids=[ref] if ref else [],
    )


def run_reminder_job(
    memory: WAMemory,
    default_audience: str,
    state: Optional[ReminderState] = None,
    **kw,
) -> int:
    """Build reminders and enqueue them for the outbound gateway to send.
    Pass a ReminderState to enable dedup (same reminder not sent every hour)."""
    intents = build_reminders(memory, default_audience, state=state, **kw)
    for i in intents:
        memory.enqueue_notification(i)
    if state:
        state.save()
    return len(intents)
