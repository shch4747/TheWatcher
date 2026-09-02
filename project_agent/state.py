"""
Defines the project page's frontmatter schema and small helpers for
reading/updating agent-owned state fields on it. Lapis pages ARE the
state store -- no separate database. See /areas/aries-dashboard.md
design notes: frontmatter is the only part the agent overwrites
wholesale; body sections get updated in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Status = Literal["active", "paused", "at_risk", "dead"]
TriggerReason = Literal["wa_update", "scheduled_sweep", "new_project", "github_activity"]


DEFAULT_FRONTMATTER: dict[str, Any] = {
    "project_id": "",
    "name": "",
    "status": "active",
    "created": "",
    "github_repos": [],
    "wa_channel": "",
    "members": [],
    "last_active_member": None,
    "healthbar": {
        "score": 100,
        "trend": "flat",
        "last_computed": None,
        "consecutive_low_checks": 0,
    },
    "healthbar_history_compact": [],
    "last_active": {"github": None, "wa": None},
    "last_agent_run": {"reason": None, "timestamp": None},
    "similarity_flags": [],
    "death_check": {
        "pending_confirmation": False,
        "asked_at": None,
        "reminder_sent": None,
    },
}


def ensure_schema(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """Fill in any missing keys with defaults, without clobbering existing
    values. Shallow-merges one level deep for nested dicts (healthbar,
    last_active, last_agent_run, death_check)."""
    fm = dict(frontmatter)
    for key, default in DEFAULT_FRONTMATTER.items():
        if key not in fm:
            fm[key] = default if not isinstance(default, dict) else dict(default)
        elif isinstance(default, dict) and isinstance(fm.get(key), dict):
            merged = dict(default)
            merged.update(fm[key])
            fm[key] = merged
    return fm


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_in_grace_period(frontmatter: dict[str, Any], grace_days: int) -> bool:
    created = frontmatter.get("created")
    if not created:
        return False
    try:
        created_dt = datetime.strptime(created, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    age_days = (datetime.now(timezone.utc) - created_dt).total_seconds() / 86400
    return age_days < grace_days


def push_healthbar_history(frontmatter: dict[str, Any], score: int, window: int) -> None:
    history = frontmatter.get("healthbar_history_compact", [])
    history.append(score)
    if len(history) > window:
        history = history[-window:]
    frontmatter["healthbar_history_compact"] = history


def update_run_metadata(frontmatter: dict[str, Any], reason: TriggerReason) -> None:
    frontmatter["last_agent_run"] = {"reason": reason, "timestamp": now_iso()}
