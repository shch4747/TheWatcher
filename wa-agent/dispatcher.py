"""
Dispatchers: how the WA agent triggers the other agents.

Two implementations of the `Dispatcher` interface (memory_interface.py):

  LoggingDispatcher
      Prints/records triggers. For dev + the first end-to-end demo, where
      the other agents aren't wired to actually run yet.

  QueueDispatcher
      Writes a trigger record into the wiki (Lapis) that the target agent
      polls and claims. This matches the club's design: agents are
      coordinated through the wiki, not by calling each other directly, so
      the execution order ("chat -> WA -> Project Agent", "run it if it
      hasn't run in x days") is expressed as durable records anyone can see.

Trigger records live at:
    inbox/triggers/<target>/<id>.md
with frontmatter:
    type: trigger
    target: research|innovation|project
    signal_type, project_ref, group, source_ids
    created_at, handled: false

De-dup: a project trigger is skipped if an *unhandled* trigger for the same
project already exists (avoids re-poking the Project Agent every batch — the
"only if it hasn't triggered recently" rule, at the coarsest useful grain).
"""
from __future__ import annotations

from datetime import datetime, timezone

from memory_interface import Dispatcher
from schemas import Signal, AgentTarget
from lapis_client import LapisClient
import mdfront


TRIGGERS_ROOT = "inbox/triggers"


class LoggingDispatcher(Dispatcher):
    def __init__(self, echo: bool = True):
        self.echo = echo
        self.triggers: list[tuple[AgentTarget, Signal]] = []

    def trigger(self, target: AgentTarget, signal: Signal) -> None:
        self.triggers.append((target, signal))
        if self.echo:
            print(f"  [dispatch] {target.value} <- {signal.type.value}: {signal.summary[:60]}")


class QueueDispatcher(Dispatcher):
    def __init__(self, client: LapisClient, dedup_project_triggers: bool = True):
        self.c = client
        self.dedup = dedup_project_triggers

    def _folder(self, target: AgentTarget) -> str:
        return f"{TRIGGERS_ROOT}/{target.value}"

    def trigger(self, target: AgentTarget, signal: Signal) -> None:
        if target == AgentTarget.NONE:
            return
        if self.dedup and signal.project_ref and self._has_open_project_trigger(target, signal.project_ref):
            return
        path = f"{self._folder(target)}/{signal.id}.md"
        meta = {
            "type": "trigger", "target": target.value,
            "signal_type": signal.type.value, "project_ref": signal.project_ref or "",
            "group": signal.group_id, "source_ids": signal.source_message_ids,
            "created_at": datetime.now(timezone.utc).isoformat(), "handled": False,
        }
        self.c.write_file(path, mdfront.dump(meta, signal.summary))

    def _has_open_project_trigger(self, target: AgentTarget, project_ref: str) -> bool:
        for path in self.c.list_files(self._folder(target) + "/"):
            if not path.endswith(".md"):
                continue
            meta, _ = mdfront.parse(self.c.read_file(path) or "")
            if meta.get("project_ref") == project_ref and not meta.get("handled"):
                return True
        return False

    # -- consumer side (the target agents call these) -------------------

    def pending_triggers(self, target: AgentTarget) -> list[dict]:
        out = []
        for path in self.c.list_files(self._folder(target) + "/"):
            if not path.endswith(".md"):
                continue
            meta, body = mdfront.parse(self.c.read_file(path) or "")
            if not meta.get("handled"):
                out.append({"path": path, **meta, "summary": body.strip()})
        return out

    def mark_handled(self, path: str) -> None:
        raw = self.c.read_file(path)
        if raw is None:
            return
        meta, body = mdfront.parse(raw)
        meta["handled"] = True
        meta["handled_at"] = datetime.now(timezone.utc).isoformat()
        self.c.write_file(path, mdfront.dump(meta, body))
