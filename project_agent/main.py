"""
Entry point / orchestrator for the Project Agent.

Intended to be invoked on a cron schedule (see config.SWEEP_INTERVAL_HOURS).
Each run:
  1. Loads all ongoing project pages from Lapis (Trigger Listener + Context Loader)
  2. For each project needing a check this cycle, recomputes the healthbar
  3. Handles death-check state machine transitions
  4. Runs similarity checks for newly created projects (+ rescans near-threshold pairs)
  5. Writes updated pages back to Lapis

This is deliberately a straightforward top-to-bottom script rather than a
long-running service -- matches the "cron + a few API calls" deployment
plan, no persistent process needed for v1.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
import state
from github_client import GitHubClient
from lapis_client import LapisClient, LapisNote
from healthbar import HealthbarInput, compute_healthbar
import writer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("project_agent")


def load_ongoing_projects(lapis: LapisClient) -> list[LapisNote]:
    notes = []
    for path in lapis.list_project_paths(config.PROJECTS_ROOT):
        note = lapis.read_note(path)
        note.frontmatter = state.ensure_schema(note.frontmatter)
        if note.frontmatter.get("status") in ("dead",):
            continue  # dead projects are frozen, not re-checked
        notes.append(note)
    return notes


def needs_check_this_cycle(note: LapisNote) -> bool:
    last_run = note.frontmatter.get("last_agent_run", {}).get("timestamp")
    if not last_run:
        return True
    last_run_dt = datetime.strptime(last_run, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    days_since = (datetime.now(timezone.utc) - last_run_dt).total_seconds() / 86400
    return days_since >= config.MAX_DAYS_SINCE_LAST_RUN


def run_healthbar_update(note: LapisNote, gh: GitHubClient) -> None:
    fm = note.frontmatter
    repos = fm.get("github_repos") or []
    activity = gh.get_activity(repos[0]) if repos else None

    # wa_last_active / active_members_last_window are expected to come from
    # the WA Agent's already-parsed output. Placeholder read from
    # frontmatter.last_active.wa until that integration is wired up.
    wa_last_active_str = fm.get("last_active", {}).get("wa")
    wa_last_active = (
        datetime.strptime(wa_last_active_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if wa_last_active_str
        else None
    )

    total_members = len(fm.get("members", [])) or 1
    active_members = len(set(fm.get("last_active_member", "").split())) if fm.get("last_active_member") else 0

    hb_input = HealthbarInput(
        github=activity,
        wa_last_active=wa_last_active,
        total_members=total_members,
        active_members_last_window=max(active_members, 1),
    )
    previous_score = fm.get("healthbar", {}).get("score")
    result = compute_healthbar(hb_input, previous_score=previous_score)

    fm["healthbar"]["score"] = result.score
    fm["healthbar"]["trend"] = result.trend
    fm["healthbar"]["last_computed"] = state.now_iso()
    state.push_healthbar_history(fm, result.score, config.HEALTHBAR_HISTORY_WINDOW)

    if activity and activity.last_commit_at:
        fm["last_active"]["github"] = activity.last_commit_at.strftime("%Y-%m-%d")

    # Death-check counter logic
    in_grace = state.is_in_grace_period(fm, config.NEW_PROJECT_GRACE_PERIOD_DAYS)
    if not in_grace and result.score <= config.DEATH_THRESHOLD_SCORE:
        fm["healthbar"]["consecutive_low_checks"] += 1
    else:
        fm["healthbar"]["consecutive_low_checks"] = 0

    if (
        fm["healthbar"]["consecutive_low_checks"] >= config.CONSECUTIVE_LOW_CHECKS_FOR_DEATH_PING
        and not fm["death_check"]["pending_confirmation"]
        and fm["status"] not in ("dead", "paused")
    ):
        fm["death_check"]["pending_confirmation"] = True
        fm["death_check"]["asked_at"] = state.now_iso()
        fm["status"] = "at_risk"
        log.info(
            "Project %s crossed death threshold %d times -- flagging for "
            "last-active-member ping via WA Agent (not sent by this script).",
            fm.get("name"),
            fm["healthbar"]["consecutive_low_checks"],
        )
        # NOTE: actually sending the WA ping is the WA Agent's job. This
        # agent only sets the death_check.pending_confirmation flag; a
        # separate handoff (e.g. WA Agent polling for this flag, or a
        # shared queue) is needed to actually message the member.

    # Update healthbar body section
    chart = None
    history = fm.get("healthbar_history_compact", [])
    if len(history) >= 2:
        chart = writer.build_mermaid_healthbar_chart(history)
    note.body = writer.update_healthbar_section(note.body, result.score, result.trend, chart)


def main():
    lapis = LapisClient()
    gh = GitHubClient()

    projects = load_ongoing_projects(lapis)
    log.info("Loaded %d ongoing projects", len(projects))

    for note in projects:
        if not needs_check_this_cycle(note):
            continue

        log.info("Checking project: %s", note.frontmatter.get("name"))
        run_healthbar_update(note, gh)
        state.update_run_metadata(note.frontmatter, reason="scheduled_sweep")
        lapis.write_note(note)
        log.info(
            "Updated %s -> score=%s trend=%s status=%s",
            note.frontmatter.get("name"),
            note.frontmatter["healthbar"]["score"],
            note.frontmatter["healthbar"]["trend"],
            note.frontmatter["status"],
        )


if __name__ == "__main__":
    main()
