"""
Central configuration for the Project Agent.

All values that depend on ARIES's actual pace (inactivity thresholds, sweep
frequency, timeouts, etc.) are deliberately kept here as named constants
rather than scattered through the codebase, since they're expected to be
tuned after discussion with the ARIES seniors and after a few real weeks
of data. Change the numbers, not the logic that uses them.
"""

import os


# ---------------------------------------------------------------------------
# Lapis connection
# ---------------------------------------------------------------------------
LAPIS_BASE_URL = os.environ.get("LAPIS_BASE_URL", "https://lapis.dvenom.in")
LAPIS_VAULT_ID = os.environ.get("LAPIS_VAULT_ID", "")
# Bearer token for headless/agent access (NOT a browser session cookie).
LAPIS_DEVICE_TOKEN = os.environ.get("LAPIS_DEVICE_TOKEN", "")

PROJECTS_ROOT = "projects"  # folder in the vault holding one subfolder per project

# ---------------------------------------------------------------------------
# GitHub connection
# ---------------------------------------------------------------------------
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# ---------------------------------------------------------------------------
# Anthropic (LLM calls: post-mortem drafts, similarity judgment)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
REASONER_MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# TUNABLE CONSTANTS — placeholders, confirm real values with senior/ARIES
# ---------------------------------------------------------------------------

# How often the scheduled sweep runs (cron cadence, informational — actual
# cron expression lives in the deploy config, this is just for reference /
# for any in-code "was this checked recently enough" math).
SWEEP_INTERVAL_HOURS = 6

# If a project hasn't been checked by the agent in this many days, the
# scheduled sweep force-runs it even with no other trigger.
MAX_DAYS_SINCE_LAST_RUN = 3

# Days of GitHub inactivity (no commits/PRs) before it starts dragging the
# healthbar down meaningfully. Used as the decay midpoint.
GITHUB_INACTIVITY_HALFLIFE_DAYS = 7

# Same, for WhatsApp channel inactivity.
WA_INACTIVITY_HALFLIFE_DAYS = 5

# Healthbar score (0-100) at or below which a project starts accumulating
# "consecutive low checks" toward a death confirmation.
DEATH_THRESHOLD_SCORE = 15

# How many consecutive low-scoring checks before the agent pings the last
# active member to confirm dead/paused/alive.
CONSECUTIVE_LOW_CHECKS_FOR_DEATH_PING = 3

# Days to wait for the last active member to respond to the death-check
# ping before sending a single reminder.
DEATH_PING_REMINDER_AFTER_DAYS = 3

# Days to wait after the reminder before giving up and marking the project
# "at_risk" instead of forcing a "dead" status.
DEATH_PING_GIVEUP_AFTER_DAYS = 7

# New projects get a grace period (no death-check accumulation) for this
# many days after creation, so a slow start isn't punished.
NEW_PROJECT_GRACE_PERIOD_DAYS = 14

# Cosine similarity threshold above which a pair of projects gets sent to
# the LLM for a real similarity judgment.
SIMILARITY_EMBEDDING_THRESHOLD = 0.75

# How many recent healthbar points to keep in the rolling compact history
# array (frontmatter `healthbar_history_compact`), used to render the graph.
HEALTHBAR_HISTORY_WINDOW = 12

# Healthbar formula weights (all deterministic, no LLM). Sum of weighted
# terms is clamped to [0, 100]. Placeholder weights — retune once real
# project data exists.
HEALTHBAR_WEIGHTS = {
    "github_inactivity": 30,   # decayed penalty
    "wa_inactivity": 20,       # decayed penalty
    "active_member_ratio": 25, # bonus, scaled by ratio
    "issue_backlog": 15,       # penalty for open > closed trend
    "commit_trend": 10,        # bonus/penalty for accelerating/decelerating commits
}
