"""
The real WAMemory backend, over Lapis's REST API.

This is the system's single wiki adapter (it implements the WA slice here;
the Innovation / Project / Research agents' methods fold into the same class
as they're wired up). It depends only on a `LapisClient`, so it's tested
against `FakeLapisClient` with no network or token.

Notes are markdown + YAML frontmatter. The folder/frontmatter convention is
centralised in LAYOUT + the frontmatter each writer emits — that's the ONE
place to adjust once the ARIES vault's real schema is confirmed. Everything
below only calls LapisClient + mdfront, so nothing else changes.

Concurrency: PUT is atomic and Lapis is immediately consistent, so
agent-vs-agent races only matter for read-modify-write on the SAME note
(e.g. two updates appended to one project page). An in-process lock guards
that here; cross-process would want optimistic concurrency (re-read via the
manifest hash before writing) — noted as a hardening TODO.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from memory_interface import WAMemory
from lapis_client import LapisClient
from schemas import Signal, NotificationIntent
import mdfront


# Lapis is ARIES's KNOWLEDGE BASE, not just a message buffer: it holds the
# durable history and information of the club — members, past & current
# projects, events, and the research corpus. The WA agent contributes to that
# durable record (LAYOUT, below).
#
# Layered ON TOP of the durable record are the per-agent INPUT PAGES: a small
# "what's new for your immediate next run" queue that each of the 3 agents
# reads and clears when it runs. So a research paper found in chat is written
# in TWO places: a permanent entry in the research corpus (history) AND a line
# on the Research agent's input page (act-on-this-next). The record persists;
# the queue is consumed.
# Paths ALIGNED to the real ARIES vault, cross-checked against research-agent
# (src/config.ts): Title-Case pages, and the research digest is the exact page
# the Research agent watches. A few (Events/, Tasks/) are still our convention
# — adjust here when confirmed. WA's own bookkeeping is hidden under dot-names.
LAYOUT = {
    "projects": "Projects",              # research-agent LAPIS_PROJECTS_PREFIX
    "events": "Events",                  # convention (not yet used by other agents)
    "tasks": "Tasks",                    # convention
    "channels": "WhatsApp/digests",      # general per-group chat digests
    "notifications": "WhatsApp/.outbox", # WA outbound queue (hidden from members)
    "state": "WhatsApp/.state",          # seen-ids etc (hidden)
}

# Per-agent input pages. INPUT_PAGES["research"] is the page the Research
# agent hashes and reacts to — writing here IS the research trigger (no
# trigger record needed). Innovation's page is our convention until confirmed.
INPUT_PAGES = {
    "research": "WhatsApp/ResearchDigest.md",   # research-agent LAPIS_WHATSAPP_PAGE_PATH
    "innovation": "Ideas/Inbox.md",
}
_DONE_TASK_STATES = {"done", "closed", "complete", "completed", "resolved"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _now().date().isoformat()


def _slug(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:max_len].strip("-") or "note")


class LapisAdapter(WAMemory):
    def __init__(self, client: LapisClient):
        self.c = client
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        self._seen_cache: set[str] | None = None

    # -- helpers ---------------------------------------------------------

    def _lock_for(self, path: str) -> threading.Lock:
        with self._global_lock:
            return self._locks.setdefault(path, threading.Lock())

    def _read_note(self, path: str) -> tuple[dict, str] | None:
        raw = self.c.read_file(path)
        return None if raw is None else mdfront.parse(raw)

    def _write_note(self, path: str, meta: dict, body: str) -> None:
        self.c.write_file(path, mdfront.dump(meta, body))

    def _list_notes(self, folder: str) -> list[tuple[str, dict, str]]:
        out = []
        for path in self.c.list_files(folder + "/"):
            if not path.endswith(".md"):
                continue
            parsed = self._read_note(path)
            if parsed is not None:
                out.append((path, parsed[0], parsed[1]))
        return out

    # -- ingestion state (dedup) ----------------------------------------

    @property
    def _seen_path(self) -> str:
        return f"{LAYOUT['state']}/wa_seen.json"

    def _load_seen(self) -> set[str]:
        if self._seen_cache is None:
            raw = self.c.read_file(self._seen_path)
            try:
                self._seen_cache = set(json.loads(raw)) if raw else set()
            except (ValueError, TypeError):
                self._seen_cache = set()
        return self._seen_cache

    def is_seen(self, message_id: str) -> bool:
        return message_id in self._load_seen()

    def mark_seen(self, message_ids: list[str]) -> None:
        if not message_ids:
            return
        with self._lock_for(self._seen_path):
            seen = self._load_seen()
            seen.update(message_ids)
            # keep it bounded — most recent 20k ids is plenty for dedup
            trimmed = list(seen)[-20000:]
            self._seen_cache = set(trimmed)
            self.c.write_file(self._seen_path, json.dumps(trimmed))

    # -- writeback from classified chat ---------------------------------

    def write_chat_digest(self, group_id: str, summary: str, source_ids: list[str]) -> None:
        path = f"{LAYOUT['channels']}/{_slug(group_id)}/{_today()}.md"
        with self._lock_for(path):
            existing = self._read_note(path)
            if existing:
                meta, body = existing
            else:
                meta = {"type": "chat-digest", "group": group_id, "date": _today()}
                body = f"# Chat digest — {group_id} — {_today()}\n"
            body += f"\n- {summary}  \n  <sub>src: {', '.join(source_ids)}</sub>"
            self._write_note(path, meta, body)

    def _append_to_input_page(self, agent: str, title: str, line: str, meta_type: str) -> None:
        """Queue one entry on an agent's input page (its next-run to-do)."""
        path = INPUT_PAGES[agent]
        with self._lock_for(path):
            existing = self._read_note(path)
            if existing:
                meta, body = existing
            else:
                meta = {"type": meta_type, "agent": agent}
                body = f"# {title}\n"
            body += f"\n- {line}"
            self._write_note(path, meta, body)

    def append_research_mention(self, signal: Signal) -> None:
        # The WA agent's research-relevant technical-discussion digest. The
        # Research agent reads THIS page (freeform), reframes it through an
        # AI/ML lens, and runs when the page content changes — so one append
        # here is both the durable record and the trigger. No corpus note, no
        # trigger record needed.
        url = signal.payload.get("url", "")
        line = (f"{_today()}: {signal.summary}" + (f" — {url}" if url else "")
                + f"  <sub>(from {signal.group_id})</sub>")
        self._append_to_input_page("research", "WhatsApp Research Digest", line, "whatsapp-digest")

    def record_idea(self, signal: Signal) -> None:
        # Append to the Innovation agent's idea inbox (freeform, one page).
        ref = f" [{signal.project_ref}]" if signal.project_ref else ""
        line = f"{_today()}:{ref} {signal.summary}  <sub>(from {signal.group_id})</sub>"
        self._append_to_input_page("innovation", "Ideas — inbox", line, "ideas-inbox")

    # -- input-page consumer side (used by the research/innovation agents) --

    def read_agent_input(self, agent: str) -> list[str]:
        """Pending queue entries for an agent's next run (does not clear)."""
        raw = self.c.read_file(INPUT_PAGES[agent])
        if not raw:
            return []
        _, body = mdfront.parse(raw)
        return [ln[2:].strip() for ln in body.splitlines() if ln.strip().startswith("- ")]

    def consume_agent_input(self, agent: str) -> list[str]:
        """Return the pending entries AND clear the queue (archiving what was
        consumed), so the next run starts empty. Call this when an agent runs.
        The durable corpus entries the lines point to are untouched."""
        path = INPUT_PAGES[agent]
        with self._lock_for(path):
            existing = self._read_note(path)
            if not existing:
                return []
            meta, body = existing
            entries = [ln[2:].strip() for ln in body.splitlines() if ln.strip().startswith("- ")]
            if entries:
                archive = path[:-3] + ".archive.md"
                aex = self._read_note(archive)
                ameta = aex[0] if aex else {"type": "input-archive", "agent": agent}
                prev = (aex[1].strip() + "\n") if aex else ""
                self._write_note(archive, ameta,
                                 prev + f"\n## consumed {_now().isoformat()}\n"
                                 + "\n".join(f"- {e}" for e in entries))
            self._write_note(path, meta, f"# {meta.get('agent', agent)} — input queue\n")
            return entries

    def append_project_update(self, signal: Signal) -> None:
        slug = _slug(signal.project_ref or signal.summary)
        path = f"{LAYOUT['projects']}/{slug}.md"
        with self._lock_for(path):
            existing = self._read_note(path)
            if existing:
                meta, body = existing
            else:
                meta = {"type": "project", "status": "active", "owner": ""}
                body = f"# {signal.project_ref or slug}\n\n## Updates\n"
            meta["last_update"] = _today()
            if "## Updates" not in body:
                body += "\n## Updates\n"
            body += f"\n- {_today()}: {signal.summary}  <sub>(via WhatsApp)</sub>"
            self._write_note(path, meta, body)

    def upsert_event(self, signal: Signal) -> None:
        path = f"{LAYOUT['events']}/{_slug(signal.summary)}-{signal.id}.md"
        meta = {
            "type": "event", "status": "upcoming",
            "date": signal.payload.get("date", ""),
            "location": signal.payload.get("location", ""),
            "group": signal.group_id, "added_at": _now().isoformat(),
        }
        self._write_note(path, meta, f"# {signal.summary}\n")

    def upsert_task(self, signal: Signal) -> None:
        path = f"{LAYOUT['tasks']}/{_slug(signal.summary)}-{signal.id}.md"
        meta = {
            "type": "task", "status": "open",
            "project": signal.project_ref or "",
            "due": signal.payload.get("due", ""),
            "assignee": signal.payload.get("assignee", ""),
            "added_at": _now().isoformat(), "group": signal.group_id,
        }
        self._write_note(path, meta, f"# {signal.summary}\n")

    # -- reads for the reminder job --------------------------------------

    def get_upcoming_events(self, within_days: int) -> list[dict[str, Any]]:
        horizon = (_now() + timedelta(days=within_days)).date().isoformat()
        today = _today()
        out = []
        for path, meta, _ in self._list_notes(LAYOUT["events"]):
            if meta.get("status") in ("done", "cancelled"):
                continue
            date = str(meta.get("date", ""))
            # include if within window, or if date is unknown/unparseable (safer to remind)
            if not date or today <= date <= horizon:
                out.append({"id": path, "title": _title_from(path, meta),
                            "date": date, "location": meta.get("location"),
                            "group": meta.get("group")})
        return out

    def get_pending_tasks(self) -> list[dict[str, Any]]:
        out = []
        for path, meta, _ in self._list_notes(LAYOUT["tasks"]):
            if str(meta.get("status", "open")).lower() in _DONE_TASK_STATES:
                continue
            out.append({"id": path, "title": _title_from(path, meta),
                        "project": meta.get("project"), "due": meta.get("due")})
        return out

    def get_projects_needing_nudge(self, stale_days: int) -> list[dict[str, Any]]:
        cutoff = (_now() - timedelta(days=stale_days)).date().isoformat()
        out = []
        for path, meta, _ in self._list_notes(LAYOUT["projects"]):
            if meta.get("status") not in (None, "active", "in-progress", "ongoing"):
                continue
            last = str(meta.get("last_update", ""))
            if last and last >= cutoff:
                continue  # updated recently enough
            days = _days_since(last) if last else stale_days
            out.append({"id": path, "title": _title_from(path, meta),
                        "days_stale": days, "group": meta.get("group")})
        return out

    # -- outbound notification queue ------------------------------------

    def enqueue_notification(self, intent: NotificationIntent) -> None:
        path = f"{LAYOUT['notifications']}/{intent.id}.md"
        meta = {
            "type": "notification", "sent": False,
            "source_agent": intent.source_agent, "audience": intent.audience,
            "urgency": getattr(intent.urgency, "value", str(intent.urgency)),
            "created_at": (intent.created_at or _now()).isoformat(),
            "source_ids": intent.source_ids,
        }
        self._write_note(path, meta, intent.payload)

    def poll_pending_notifications(self) -> list[NotificationIntent]:
        out = []
        for path, meta, body in self._list_notes(LAYOUT["notifications"]):
            if meta.get("sent"):
                continue
            out.append(NotificationIntent(
                id=path.rsplit("/", 1)[-1][:-3], source_agent=meta.get("source_agent", ""),
                audience=meta.get("audience", ""), payload=body.strip(),
                source_ids=meta.get("source_ids", []) or [],
            ))
        return out

    def mark_notification_sent(self, intent_id: str) -> None:
        path = f"{LAYOUT['notifications']}/{intent_id}.md"
        with self._lock_for(path):
            existing = self._read_note(path)
            if not existing:
                return
            meta, body = existing
            meta["sent"] = True
            meta["sent_at"] = _now().isoformat()
            self._write_note(path, meta, body)

    # -- per-group rolling context --------------------------------------

    def _group_ctx_path(self, group_id: str) -> str:
        return f"{LAYOUT['channels']}/{_slug(group_id)}/_context.md"

    def get_group_context(self, group_id: str) -> str:
        raw = self.c.read_file(self._group_ctx_path(group_id))
        return mdfront.parse(raw)[1].strip() if raw else ""

    def update_group_context(self, group_id: str, note: str) -> None:
        path = self._group_ctx_path(group_id)
        with self._lock_for(path):
            existing = self._read_note(path)
            meta = existing[0] if existing else {"type": "group-context", "group": group_id}
            lines = (existing[1].strip().splitlines() if existing else [])
            lines.append(f"- {_today()}: {note}")
            self._write_note(path, meta, "\n".join(lines[-30:]))   # keep it bounded


# -- small helpers ----------------------------------------------------------

def _title_from(path: str, meta: dict) -> str:
    if meta.get("title"):
        return meta["title"]
    return path.rsplit("/", 1)[-1][:-3]


def _days_since(iso_date: str) -> int:
    try:
        d = datetime.fromisoformat(iso_date).date()
        return (_now().date() - d).days
    except ValueError:
        return 0
