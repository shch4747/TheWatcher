"""
Pure, deterministic healthbar scoring. No LLM calls here on purpose —
this needs to be cheap, fast, and easy to unit test / retune once real
weights come back from the ARIES senior discussion.

Score is 0-100. Higher = healthier.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from config import (
    GITHUB_INACTIVITY_HALFLIFE_DAYS,
    HEALTHBAR_WEIGHTS,
    WA_INACTIVITY_HALFLIFE_DAYS,
)
from github_client import RepoActivity


@dataclass
class HealthbarInput:
    github: RepoActivity | None
    wa_last_active: datetime | None
    total_members: int
    active_members_last_window: int  # committed OR messaged in the window
    now: datetime | None = None


@dataclass
class HealthbarResult:
    score: int
    trend: str  # "up" | "down" | "flat"
    breakdown: dict[str, float]


def _decay_penalty(days_inactive: float, halflife_days: float, weight: float) -> float:
    """Exponential decay: full weight at 0 days inactive, half the weight
    at `halflife_days`, approaching 0 penalty impact... actually inverted:
    this returns how much of `weight` survives (i.e. how much penalty to
    apply), so more inactivity -> higher penalty -> lower score.
    """
    if days_inactive <= 0:
        return 0.0
    decay_factor = 1 - math.pow(0.5, days_inactive / halflife_days)
    return weight * decay_factor


def compute_healthbar(inp: HealthbarInput, previous_score: int | None = None) -> HealthbarResult:
    now = inp.now or datetime.now(timezone.utc)
    w = HEALTHBAR_WEIGHTS
    breakdown: dict[str, float] = {}

    score = 100.0

    # GitHub inactivity penalty
    if inp.github and inp.github.last_commit_at:
        days_inactive = (now - inp.github.last_commit_at).total_seconds() / 86400
    else:
        days_inactive = 999  # no repo linked / no commits ever -> treat as fully stale
    gh_penalty = _decay_penalty(days_inactive, GITHUB_INACTIVITY_HALFLIFE_DAYS, w["github_inactivity"])
    breakdown["github_inactivity_penalty"] = -gh_penalty
    score -= gh_penalty

    # WA inactivity penalty
    if inp.wa_last_active:
        wa_days_inactive = (now - inp.wa_last_active).total_seconds() / 86400
    else:
        wa_days_inactive = 999
    wa_penalty = _decay_penalty(wa_days_inactive, WA_INACTIVITY_HALFLIFE_DAYS, w["wa_inactivity"])
    breakdown["wa_inactivity_penalty"] = -wa_penalty
    score -= wa_penalty

    # Active member ratio -- this is a bonus term, scaled by weight, so an
    # empty/ghost project doesn't get it, and full participation gets full weight.
    ratio = (
        inp.active_members_last_window / inp.total_members if inp.total_members > 0 else 0.0
    )
    ratio_bonus = w["active_member_ratio"] * ratio - w["active_member_ratio"] / 2
    # centered: ratio=0.5 is neutral, ratio=1 is full bonus, ratio=0 is full penalty
    breakdown["active_member_ratio_adj"] = ratio_bonus
    score += ratio_bonus

    # Issue backlog: opened faster than closed -> penalty
    if inp.github:
        backlog_delta = inp.github.opened_issues_last_30d - inp.github.closed_issues_last_30d
        backlog_penalty = min(max(backlog_delta, 0), 10) / 10 * w["issue_backlog"]
    else:
        backlog_penalty = 0.0
    breakdown["issue_backlog_penalty"] = -backlog_penalty
    score -= backlog_penalty

    # Commit trend: accelerating vs decelerating vs previous window
    if inp.github and inp.github.commits_prev_window > 0:
        trend_ratio = inp.github.commits_this_window / inp.github.commits_prev_window
        trend_adj = w["commit_trend"] * (min(trend_ratio, 2.0) - 1.0) / 1.0
    elif inp.github and inp.github.commits_this_window > 0:
        trend_adj = w["commit_trend"]  # went from nothing to something -> full bonus
    else:
        trend_adj = 0.0
    breakdown["commit_trend_adj"] = trend_adj
    score += trend_adj

    score = max(0, min(100, round(score)))

    if previous_score is None:
        trend = "flat"
    elif score > previous_score:
        trend = "up"
    elif score < previous_score:
        trend = "down"
    else:
        trend = "flat"

    return HealthbarResult(score=score, trend=trend, breakdown=breakdown)
