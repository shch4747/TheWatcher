"""
The single interface the Innovation Agent talks to. Everything it knows
comes through this — it never touches Lapis (or Postgres, or anything
else) directly.

Swap `LapisAdapter`'s internals for whatever Lapis's real file layout /
frontmatter fields turn out to be; nothing else in the agent needs to
change, because the rest of the codebase only ever calls MemoryInterface
methods.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional
import re

try:
    import yaml  # pyyaml — Obsidian-style frontmatter is YAML between --- lines
except ImportError:
    yaml = None


class MemoryInterface(ABC):
    """What the Innovation Agent is allowed to ask memory for."""

    # -- triggers --
    @abstractmethod
    def poll_new_project_closures(self, since: str) -> list[str]:
        """Project IDs closed since `since`."""

    @abstractmethod
    def poll_new_research_deepdives(self, since: str) -> list[str]:
        """Research entry IDs that got a full deep-dive since `since`."""

    # -- grounded --
    @abstractmethod
    def get_failure_cause(self, project_id: str) -> Optional[str]:
        ...

    @abstractmethod
    def get_all_closed_project_causes(self) -> dict[str, str]:
        """project_id -> failure_cause, for closed projects only."""

    @abstractmethod
    def get_capability_usage(self) -> dict[str, list[str]]:
        """capability/technology name -> list of project IDs that used it."""

    # -- bridged --
    @abstractmethod
    def get_research_entry(self, entry_id: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def get_capabilities_far_from(self, topic: str, k: int = 5) -> list[dict[str, Any]]:
        """Capabilities/past projects semantically distant from `topic`."""

    # -- free --
    @abstractmethod
    def get_org_snapshot(self) -> str:
        """Compact prose summary of ARIES's current shape."""

    @abstractmethod
    def get_light_research_digest(self, since: str) -> list[dict[str, Any]]:
        """Broadly-scanned (not necessarily deep-dived) research entries."""

    # -- events --
    @abstractmethod
    def get_event_history(self) -> list[dict[str, Any]]:
        """Past events/workshops/hackathons with attendance and feedback.
        Any stable order is fine — the lane doesn't assume ranking."""

    # -- feedback loop --
    @abstractmethod
    def get_recent_feedback(
        self, lane: str, limit: int = 5, verdict: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Human up/down/neutral verdicts on past pitches from this lane,
        most recent first. This is the up/down feedback.md loop, distinct
        from the pursue/defer/reject outcome tracked via write_outcome —
        that one is the club's eventual final call on a pitch; this one
        is the faster "did this look promising" signal meant to condition
        future generation. Empty list is a normal cold-start result, not
        an error — callers should degrade gracefully."""

    # -- gate --
    @abstractmethod
    def get_pitch_history(self) -> list[dict[str, Any]]:
        ...

    # -- writeback --
    @abstractmethod
    def write_pitch(self, pitch) -> None:
        ...

    @abstractmethod
    def write_outcome(self, pitch_id: str, outcome: str, note: str = "") -> None:
        ...


class MockMemory(MemoryInterface):
    """
    Hand-written fake data so the pipeline is runnable and testable
    before Lapis is wired in for real. See demo.py.
    """

    def __init__(self):
        self._closed_causes = {
            "proj-auth-a": "underestimated OAuth edge cases",
            "proj-auth-b": "underestimated OAuth edge cases",
        }
        self._capability_usage = {
            "OCR": ["proj-ocr-a"],
            "RAG": ["proj-rag-a", "proj-rag-b", "proj-rag-c", "proj-rag-d"],
            "WebRTC": ["proj-callgpt"],
        }
        self._research = {
            "res-1": {
                "id": "res-1",
                "title": "Weak supervision for document layout understanding",
                "topic": "document understanding",
            }
        }
        self._light_digest = [
            {"id": "res-2", "title": "Neuroevolution for legged locomotion in cluttered terrain"},
            {"id": "res-3", "title": "Self-play curricula for negotiation agents"},
        ]
        self._events = [
            {
                "id": "evt-hackathon-2025-11",
                "title": "Overnight Hardware Hackathon",
                "type": "hackathon",
                "attendance": 42,
                "feedback_score": 4.6,
                "feedback_notes": "Highest turnout of the semester; multiple people asked for a sequel.",
            },
            {
                "id": "evt-ml-talk-2025-09",
                "title": "Intro to Diffusion Models talk",
                "type": "talk",
                "attendance": 11,
                "feedback_score": 2.8,
                "feedback_notes": "Low turnout; feedback said too theoretical, wanted a hands-on component.",
            },
            {
                "id": "evt-workshop-2025-08",
                "title": "Git & GitHub onboarding workshop",
                "type": "workshop",
                "attendance": 35,
                "feedback_score": 4.1,
                "feedback_notes": "Well-received by first-years; hasn't been repeated since new members joined this sem.",
            },
        ]
        self._pitch_history: list[dict[str, Any]] = []

        # Deliberately seeded for only ONE lane, so testing exercises both
        # the "has feedback" path (grounded) and the cold-start path
        # (bridged/free/events all return []) — real state early on will
        # look like this too, not uniformly empty or uniformly full.
        self._feedback: dict[str, list[dict[str, Any]]] = {
            "grounded": [
                {
                    "idea_id": "mock-g1",
                    "lane": "grounded",
                    "idea_summary": "Shared OAuth edge-case test harness for CI",
                    "verdict": "up",
                    "reason": "Directly useful, we should build this",
                    "reviewed_at": "2026-09-01T10:00:00+00:00",
                },
                {
                    "idea_id": "mock-g2",
                    "lane": "grounded",
                    "idea_summary": "Mandatory design-doc template before any new project starts",
                    "verdict": "down",
                    "reason": "Too much process overhead for a student club",
                    "reviewed_at": "2026-09-02T10:00:00+00:00",
                },
            ],
        }

        # Raw content for the verification loop to check claims against —
        # mirrors the IDs used elsewhere above, just as prose a model can read.
        self._records = {
            "proj-auth-a": "Project proj-auth-a: an auth system. Closed. Failure cause: underestimated OAuth edge cases.",
            "proj-auth-b": "Project proj-auth-b: an auth system. Closed. Failure cause: underestimated OAuth edge cases.",
            "proj-ocr-a": "Project proj-ocr-a: ARIES's OCR project. The only project that has used OCR so far.",
            "proj-rag-a": "Project proj-rag-a: a RAG project.",
            "proj-rag-b": "Project proj-rag-b: a RAG project.",
            "proj-rag-c": "Project proj-rag-c: a RAG project.",
            "proj-rag-d": "Project proj-rag-d: a RAG project.",
            "proj-callgpt": "Project proj-callgpt: ARIES's WebRTC-based voice pipeline project.",
            "res-1": "Research entry res-1: 'Weak supervision for document layout understanding' — topic: document understanding.",
            "evt-hackathon-2025-11": "Event evt-hackathon-2025-11: Overnight Hardware Hackathon. Attendance 42, feedback score 4.6/5. Highest turnout of the semester; multiple people asked for a sequel.",
            "evt-ml-talk-2025-09": "Event evt-ml-talk-2025-09: Intro to Diffusion Models talk. Attendance 11, feedback score 2.8/5. Too theoretical, wanted a hands-on component.",
            "evt-workshop-2025-08": "Event evt-workshop-2025-08: Git & GitHub onboarding workshop. Attendance 35, feedback score 4.1/5. Well-received by first-years, hasn't been repeated since new members joined.",
        }

    def poll_new_project_closures(self, since: str) -> list[str]:
        return ["proj-auth-b"]

    def poll_new_research_deepdives(self, since: str) -> list[str]:
        return ["res-1"]

    def get_failure_cause(self, project_id: str) -> Optional[str]:
        return self._closed_causes.get(project_id)

    def get_all_closed_project_causes(self) -> dict[str, str]:
        return dict(self._closed_causes)

    def get_capability_usage(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._capability_usage.items()}

    def get_research_entry(self, entry_id: str) -> dict[str, Any]:
        return self._research.get(entry_id, {})

    def get_capabilities_far_from(self, topic: str, k: int = 5) -> list[dict[str, Any]]:
        return [{"name": "neuroevolution (Darwin Bot)", "distance": "far"}]

    def get_org_snapshot(self) -> str:
        return "ARIES: ML/AI club, active in RAG, voice pipelines, neuroevolution, document OCR."

    def get_light_research_digest(self, since: str) -> list[dict[str, Any]]:
        return list(self._light_digest)

    def get_event_history(self) -> list[dict[str, Any]]:
        return list(self._events)

    def get_recent_feedback(
        self, lane: str, limit: int = 5, verdict: Optional[str] = None
    ) -> list[dict[str, Any]]:
        entries = self._feedback.get(lane, [])
        if verdict:
            entries = [e for e in entries if e["verdict"] == verdict]
        entries = sorted(entries, key=lambda e: e.get("reviewed_at") or "", reverse=True)
        return entries[:limit]

    def get_pitch_history(self) -> list[dict[str, Any]]:
        return list(self._pitch_history)

    def write_pitch(self, pitch) -> None:
        self._pitch_history.append({"id": pitch.id, "title": pitch.idea.title})

    def write_outcome(self, pitch_id: str, outcome: str, note: str = "") -> None:
        for p in self._pitch_history:
            if p["id"] == pitch_id:
                p["outcome"] = outcome
                p["note"] = note

    def get_record(self, record_id: str) -> Optional[dict[str, Any]]:
        content = self._records.get(record_id)
        return {"id": record_id, "content": content} if content else None


class LapisAdapter(MemoryInterface):
    """
    Skeleton for the real backend: a directory of Obsidian-style markdown
    files with YAML frontmatter and [[wikilinks]].

    Assumed vault layout (adjust to match Lapis's actual schema — this
    part is a guess, everything else in the codebase is not):

        vault/projects/*.md   frontmatter: status: active|closed, cause: ..., technologies: [...]
        vault/research/*.md   frontmatter: depth: light|deep, topic: ..., added_at: ...
        vault/pitches/*.md    frontmatter: origin, status, created_at
        vault/events/*.md     frontmatter: type, date, attendance: int, feedback_score: float,
                               feedback_notes: ... (this one is a bigger guess than the rest —
                               event feedback isn't modeled in Lapis yet as far as this codebase
                               knows, so treat this layout as a starting proposal, not a fact)

    Every method here is a real starting implementation against that
    assumed layout, not a NotImplementedError placeholder — so once the
    vault layout is confirmed, this is a find-and-adjust job, not a
    rewrite.
    """

    def __init__(self, vault_path: str):
        self.vault = Path(vault_path)

    def _read_notes(self, subdir: str) -> list[dict[str, Any]]:
        notes = []
        folder = self.vault / subdir
        if not folder.exists():
            return notes
        for f in folder.glob("*.md"):
            frontmatter, body = self._split_frontmatter(f.read_text(encoding="utf-8"))
            frontmatter["_id"] = f.stem
            frontmatter["_body"] = body
            frontmatter["_links"] = re.findall(r"\[\[([^\]]+)\]\]", body)
            notes.append(frontmatter)
        return notes

    @staticmethod
    def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
        if yaml and text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                meta = yaml.safe_load(parts[1]) or {}
                return meta, parts[2]
        return {}, text

    def poll_new_project_closures(self, since: str) -> list[str]:
        return [
            n["_id"] for n in self._read_notes("projects")
            if n.get("status") == "closed" and n.get("closed_at", "") > since
        ]

    def poll_new_research_deepdives(self, since: str) -> list[str]:
        return [
            n["_id"] for n in self._read_notes("research")
            if n.get("depth") == "deep" and n.get("added_at", "") > since
        ]

    def get_failure_cause(self, project_id: str) -> Optional[str]:
        for n in self._read_notes("projects"):
            if n["_id"] == project_id:
                return n.get("cause")
        return None

    def get_all_closed_project_causes(self) -> dict[str, str]:
        return {
            n["_id"]: n["cause"]
            for n in self._read_notes("projects")
            if n.get("status") == "closed" and n.get("cause")
        }

    def get_capability_usage(self) -> dict[str, list[str]]:
        # FIX: this method previously existed as get_capability_usage_counts,
        # returning {tech: count} — a different name AND a different shape
        # than the abstract method on MemoryInterface requires ({tech: [ids]}).
        # LapisAdapter could never actually be instantiated as a result
        # (ABCMeta blocks it) until this was renamed and reshaped to match.
        usage: dict[str, list[str]] = {}
        for n in self._read_notes("projects"):
            for tech in n.get("technologies", []):
                usage.setdefault(tech, []).append(n["_id"])
        return usage

    def get_research_entry(self, entry_id: str) -> dict[str, Any]:
        for n in self._read_notes("research"):
            if n["_id"] == entry_id:
                return n
        return {}

    def get_capabilities_far_from(self, topic: str, k: int = 5) -> list[dict[str, Any]]:
        # TODO: real embedding-based distance (pgvector, or embed the
        # note bodies directly). For now: everything not tagged with
        # `topic` counts as "far", capped at k.
        return [n for n in self._read_notes("projects") if topic not in n.get("tags", [])][:k]

    def get_org_snapshot(self) -> str:
        active = [n["_id"] for n in self._read_notes("projects") if n.get("status") == "active"]
        return f"Active projects: {', '.join(active) if active else 'none'}."

    def get_light_research_digest(self, since: str) -> list[dict[str, Any]]:
        return [n for n in self._read_notes("research") if n.get("added_at", "") > since]

    def get_event_history(self) -> list[dict[str, Any]]:
        # Unconfirmed layout, see class docstring. Adjust field names
        # once Lapis's actual events schema exists.
        return self._read_notes("events")

    def read_file(self, path: str) -> Optional[str]:
        """Generic single-file read, distinct from _read_notes (which
        globs a directory of one-record-per-file notes). The feedback
        log is the opposite shape: one file per lane holding a growing
        list of entries — so it needs plain path-based read/write, not
        the per-note pattern the rest of this class uses."""
        full = self.vault / path
        if not full.exists():
            return None
        return full.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> None:
        full = self.vault / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    def get_recent_feedback(
        self, lane: str, limit: int = 5, verdict: Optional[str] = None
    ) -> list[dict[str, Any]]:
        # Delegates to lapis_feedback_store rather than re-parsing the
        # frontmatter format here — that module is the single definition
        # of the feedback file format; duplicating the parsing logic in
        # two places is exactly how the two versions quietly drift apart.
        from lapis_feedback_store import get_recent_feedback as _get_recent_feedback
        return _get_recent_feedback(self, lane, limit=limit, verdict=verdict)

    def get_pitch_history(self) -> list[dict[str, Any]]:
        return self._read_notes("pitches")

    @abstractmethod
    def get_record(self, record_id: str) -> Optional[dict[str, Any]]:
        """Generic lookup by ID, across whatever record types memory holds
        (project, research entry, etc). Returns enough content to check a
        claim against — None if the ID doesn't exist at all, which is
        itself a meaningful signal (a cited ID that was invented)."""

    def write_pitch(self, pitch) -> None:
        folder = self.vault / "pitches"
        folder.mkdir(parents=True, exist_ok=True)
        fm = {
            "type": "pitch",
            "origin": pitch.idea.origin.value,
            "status": pitch.status.value,
            "created_at": pitch.created_at.isoformat(),
        }
        body = f"# {pitch.idea.title}\n\n{pitch.idea.statement}\n"
        header = yaml.safe_dump(fm) if yaml else str(fm)
        (folder / f"{pitch.id}.md").write_text(f"---\n{header}---\n{body}", encoding="utf-8")

    def write_outcome(self, pitch_id: str, outcome: str, note: str = "") -> None:
        f = self.vault / "pitches" / f"{pitch_id}.md"
        if not f.exists():
            return
        fm, body = self._split_frontmatter(f.read_text(encoding="utf-8"))
        fm["status"] = outcome
        fm["outcome_note"] = note
        header = yaml.safe_dump(fm) if yaml else str(fm)
        f.write_text(f"---\n{header}---\n{body}", encoding="utf-8")

    def get_record(self, record_id: str) -> Optional[dict[str, Any]]:
        # A cited ID could be a project, research entry, or event — check
        # all three rather than assuming, since evidence can point at any.
        for subdir in ("projects", "research", "events"):
            for n in self._read_notes(subdir):
                if n["_id"] == record_id:
                    return n
        return None