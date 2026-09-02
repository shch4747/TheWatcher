# ARIES Project Agent — v0

Implements the design from the dashboard planning discussion: reads project
pages from Lapis, pulls GitHub activity, computes a deterministic healthbar,
runs the death-check state machine, and writes updates back to Lapis.

## Setup

```bash
pip install -r requirements.txt
export LAPIS_VAULT_ID=...
export LAPIS_DEVICE_TOKEN=...      # Bearer token for headless access — confirm
                                    # issuance flow with the Lapis maintainer
export GITHUB_TOKEN=...
export ANTHROPIC_API_KEY=...
python main.py
```

Run on a cron schedule (see `config.SWEEP_INTERVAL_HOURS` for the intended
cadence — actual cron expression lives in your deploy config, e.g. a
GitHub Actions scheduled workflow).

## What's implemented

- `config.py` — all tunable thresholds/weights in one place (placeholders —
  confirm real values with your senior before relying on death/at-risk
  transitions)
- `lapis_client.py` — REST client against the documented Lapis API
  (manifest, read/write files, search, backlinks), plus frontmatter/body
  parsing
- `github_client.py` — pulls commit recency, committers, issue backlog,
  commit-trend windows, contributors
- `healthbar.py` — pure deterministic scoring (no LLM), exponential decay
  on inactivity, active-member-ratio bonus, issue backlog and commit trend
  terms
- `state.py` — frontmatter schema + grace period / history helpers
- `writer.py` — idempotent, section-marker-based body updates (never
  regenerates human-written sections like Goal/Milestones), Mermaid
  healthbar chart builder
- `reasoner.py` — the only LLM-call surface: post-mortem drafting,
  similarity judgment
- `main.py` — orchestration loop for the healthbar-update + death-check
  path

## What's stubbed / not yet wired up

- **WA Agent handoff**: this agent sets `death_check.pending_confirmation`
  and drops a log line, but does not itself send the WhatsApp ping — that's
  the WA Agent's job. Needs a shared signal (the WA Agent polling Lapis for
  this flag, or a small queue) to actually connect the two agents.
- **Similarity matching pipeline**: `reasoner.judge_similarity()` exists,
  but the embedding-based first pass (title+goal cosine similarity across
  ongoing projects, rescanning only near-threshold pairs) isn't wired into
  `main.py` yet — needs an embeddings call (e.g. Voyage) and a small
  vector-compare loop.
- **New Project Formation trigger**: no listener yet for "a new page
  appeared under projects/" — first-pass similarity check and GitHub-repo
  linking should fire from that event once the manifest-diffing logic is
  added to `main.py`.
- **Post-mortem full flow**: `reasoner.draft_post_mortem()` exists and
  `writer.append_post_mortem()` will write it, but the actual "ask member
  for reason, get their answer, draft, send back for confirm" conversation
  loop lives on the WA Agent side, not here.
- **Lapis auth**: assumes a long-lived Bearer token is already available.
  The README's device-code flow is described for the Obsidian plugin
  specifically — worth confirming with your senior whether headless
  scripts get tokens the same way.

## Design docs

The full architecture discussion (triggers, schema, death-confirmation
flow, similarity rules) is recorded separately — this repo is the
implementation, not the source of truth for *why* things work this way.
