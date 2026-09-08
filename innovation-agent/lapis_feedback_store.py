"""
lapis_feedback_store.py — feedback storage lives entirely on Lapis,
not on local disk. One markdown page per lane, each holding a
growing list of feedback entries (YAML frontmatter, same pattern as
your WhatsApp output files).

No SQLite, no local database file, no per-machine state. Any agent
process, on any machine, reads and writes the same shared Lapis
pages — that's the point of using Lapis as the backend at all.

Path convention: Ideas/Feedback/<lane>.md — one file per lane
(remedial / bridged / frontier / event), mirroring the per-agent
output file pattern from WhatsApp/output/<agent>.md, for the same
reason: avoids read-modify-write contention across lanes.

There is no "processed" flag and no separate ingestion step. A Lapis
page IS the permanent record — reading it doesn't consume it, so
there's nothing to migrate and nothing to duplicate.
"""

import mdfront  # same YAML-frontmatter helper your WA output code uses
from datetime import datetime, timezone
from collections import Counter
from typing import Optional

FEEDBACK_DIR = "Ideas/Feedback"

DEFAULT_BODY = (
    "# Feedback log\n\n"
    "Fill in `verdict` (up / down / neutral) and optionally `reason` "
    "for any idea below with `verdict: null`. Everything else here "
    "is read-only history — no need to touch it.\n"
)


def _path(lane: str) -> str:
    return f"{FEEDBACK_DIR}/{lane}.md"


def _read_entries(lapis_client, lane: str):
    raw = lapis_client.read_file(_path(lane))
    if raw:
        meta, body = mdfront.parse(raw)
    else:
        meta, body = {"type": "feedback-log", "lane": lane, "entries": []}, DEFAULT_BODY
    return meta, body, (meta.get("entries", []) or [])


def _write_entries(lapis_client, lane: str, meta: dict, body: str, entries: list) -> None:
    meta["entries"] = entries
    lapis_client.write_file(_path(lane), mdfront.dump(meta, body))


def publish_pending_review(lapis_client, *, idea_id: str, lane: str, idea_summary: str) -> None:
    """Called by the gate/publish step for every idea that survives
    the gate. Appends a blank stub a human fills in directly in
    Obsidian."""
    meta, body, entries = _read_entries(lapis_client, lane)
    entries.append({
        "idea_id": idea_id,
        "lane": lane,
        "idea_summary": idea_summary,
        "verdict": None,
        "reason": None,
        "reviewer": None,
        "reviewed_at": None,
        "published_at": datetime.now(timezone.utc).isoformat(),
    })
    _write_entries(lapis_client, lane, meta, body, entries)


def get_recent_feedback(
    lapis_client, lane: str, limit: int = 5, verdict: Optional[str] = None
) -> list[dict]:
    """v1 retrieval: most recent N *reviewed* entries for a lane
    (verdict already filled in), optionally filtered to up/down/
    neutral. Reads straight off Lapis every time — no local index to
    keep in sync, no staleness risk."""
    _, _, entries = _read_entries(lapis_client, lane)
    reviewed = [e for e in entries if e.get("verdict")]
    if verdict:
        reviewed = [e for e in reviewed if e["verdict"] == verdict]
    reviewed.sort(key=lambda e: e.get("reviewed_at") or "", reverse=True)
    return reviewed[:limit]


def get_stats(lapis_client, lanes: list[str]) -> dict:
    """Up/down/neutral counts per lane. Plain Python aggregation —
    a few hundred dicts don't need a database to tally."""
    counts: Counter = Counter()
    for lane in lanes:
        _, _, entries = _read_entries(lapis_client, lane)
        for e in entries:
            if e.get("verdict"):
                counts[(lane, e["verdict"])] += 1
    return dict(counts)