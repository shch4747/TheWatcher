"""
The memory surface the WA Agent talks to — the WA-specific extension of
the system-wide MemoryInterface.

DESIGN NOTE. In the real repo these methods fold into the ONE shared
`MemoryInterface` that the Innovation / Project / Research agents already
use (see innovation-agent/memory_interface.py), with a single
`LapisAdapter` implementing all of them. They live here for now so the WA
agent is runnable and testable in isolation before that merge. When Lapis's
real vault schema is confirmed, only the adapter changes — nothing above it.

Two collaborators, both defined against interfaces so they can be mocked:
  - WAMemory   : reading/writing the wiki + the ingestion/outbound state
  - Dispatcher : triggering the other agents (research/innovation/project)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from schemas import Signal, NotificationIntent, AgentTarget


# --------------------------------------------------------------------------
# Wiki + state
# --------------------------------------------------------------------------

class WAMemory(ABC):
    """Everything the WA agent is allowed to read from / write to memory."""

    # -- ingestion state (dedup + resume) --
    @abstractmethod
    def is_seen(self, message_id: str) -> bool: ...

    @abstractmethod
    def mark_seen(self, message_ids: list[str]) -> None: ...

    # -- writeback from classified chat (WA agent is a SOURCE of these) --
    @abstractmethod
    def write_chat_digest(self, group_id: str, summary: str, source_ids: list[str]) -> None: ...

    @abstractmethod
    def append_research_mention(self, signal: Signal) -> None: ...

    @abstractmethod
    def record_idea(self, signal: Signal) -> None: ...

    @abstractmethod
    def append_project_update(self, signal: Signal) -> None: ...

    @abstractmethod
    def upsert_event(self, signal: Signal) -> None: ...

    @abstractmethod
    def upsert_task(self, signal: Signal) -> None: ...

    # -- reads for the reminder / task-chasing job --
    @abstractmethod
    def get_upcoming_events(self, within_days: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_pending_tasks(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_projects_needing_nudge(self, stale_days: int) -> list[dict[str, Any]]: ...

    # -- outbound notification queue (the gateway) --
    @abstractmethod
    def enqueue_notification(self, intent: NotificationIntent) -> None: ...

    @abstractmethod
    def poll_pending_notifications(self) -> list[NotificationIntent]: ...

    @abstractmethod
    def mark_notification_sent(self, intent_id: str) -> None: ...

    # -- per-group rolling context (for tailoring outbound messages) --
    @abstractmethod
    def get_group_context(self, group_id: str) -> str: ...

    @abstractmethod
    def update_group_context(self, group_id: str, note: str) -> None: ...


class Dispatcher(ABC):
    """Triggers another agent with a payload. Real impl enqueues a job /
    calls the agent's pipeline entry point; mock just records the call."""

    @abstractmethod
    def trigger(self, target: AgentTarget, signal: Signal) -> None: ...


# --------------------------------------------------------------------------
# Mock implementations for local testing (no Lapis, no queue, no network)
# --------------------------------------------------------------------------

class MockWAMemory(WAMemory):
    def __init__(self, upcoming_events=None, pending_tasks=None, stale_projects=None):
        self._seen: set[str] = set()
        self.writes: list[tuple[str, Any]] = []          # (method, signal/summary) log
        self.notifications: list[NotificationIntent] = []
        self._upcoming = upcoming_events or []
        self._pending = pending_tasks or []
        self._stale = stale_projects or []
        self._group_ctx: dict[str, list[str]] = {}

    def is_seen(self, message_id: str) -> bool:
        return message_id in self._seen

    def mark_seen(self, message_ids: list[str]) -> None:
        self._seen.update(message_ids)

    def write_chat_digest(self, group_id, summary, source_ids):
        self.writes.append(("write_chat_digest", summary))

    def append_research_mention(self, signal):
        self.writes.append(("append_research_mention", signal))

    def record_idea(self, signal):
        self.writes.append(("record_idea", signal))

    def append_project_update(self, signal):
        self.writes.append(("append_project_update", signal))

    def upsert_event(self, signal):
        self.writes.append(("upsert_event", signal))

    def upsert_task(self, signal):
        self.writes.append(("upsert_task", signal))

    def get_upcoming_events(self, within_days):
        return list(self._upcoming)

    def get_pending_tasks(self):
        return list(self._pending)

    def get_projects_needing_nudge(self, stale_days):
        return list(self._stale)

    def enqueue_notification(self, intent):
        self.notifications.append(intent)

    def poll_pending_notifications(self):
        return [n for n in self.notifications if not n.sent]

    def mark_notification_sent(self, intent_id):
        for n in self.notifications:
            if n.id == intent_id:
                n.sent = True

    def get_group_context(self, group_id):
        return "\n".join(self._group_ctx.get(group_id, []))

    def update_group_context(self, group_id, note):
        self._group_ctx.setdefault(group_id, []).append(note)
        self._group_ctx[group_id] = self._group_ctx[group_id][-30:]


class MockDispatcher(Dispatcher):
    def __init__(self):
        self.triggers: list[tuple[AgentTarget, Signal]] = []

    def trigger(self, target: AgentTarget, signal: Signal) -> None:
        self.triggers.append((target, signal))


# The real backend lives in lapis_adapter.py (LapisAdapter over Lapis's REST
# API). It's a separate module to keep this file focused on the interface +
# mocks and to avoid importing the HTTP client here. Import it where you wire
# things up:
#
#     from lapis_client import HttpLapisClient
#     from lapis_adapter import LapisAdapter
#     memory = LapisAdapter(HttpLapisClient())   # reads LAPIS_* env vars
#
# LapisAdapter is the system's single wiki adapter — the Innovation / Project
# / Research agents' methods fold into that same class as they're wired up.
