# Watcher — ARIES WhatsApp → wiki bot

Watcher is a bot on a dedicated WhatsApp number that sits in ARIES's
groups, reads them in batches, organises what it sees into per-group
Threads, and keeps the club's Lapis wiki current: task/decision/resource
extraction, thread summaries, and a chat interface members can address
directly. See [`docs/Spec - Watcher v1.md`](docs/Spec%20-%20Watcher%20v1.md)
for the full problem statement and design, [`docs/Plan - Watcher v1.md`](docs/Plan%20-%20Watcher%20v1.md)
for the phase-by-phase build, and [`docs/Architecture.md`](docs/Architecture.md)
for a diagram of how the pieces talk to each other.

This is the v1 build (Phases 0-6 of the Plan, all landed). Two earlier
prototypes - `agents/innovation-agent/` and `agents/research-agent/` -
predate this design and are **frozen** until v1 ships; see
[ADR-0010](docs/adr/0010-freeze-innovation-research-for-v1.md).

## Repo layout

```
shared/            cross-cutting packages every agent depends on
  gateway/         gowa I/O: webhook intake, send, commands, identity, proposals
  wiki/            schema, parser/serialiser, Lapis client, lint, derived regen, templates
  models/          Decision/Worker/Mentor model tiers, skills, benchmark harness
  scheduler/       job registry, run ledger, triggers, locks
  cms/             member lookup (ADR-0009)
  db.py            the one SQLite schema (ADR-0005: one app, one database)
  http_adapter.py  thin JSON pass-through over the above, for out-of-process callers
agents/
  wa_agent/        batch cutting, thread assignment/writing, lifecycle, Chat Agent
  project_agent/   Item upsert into initiative pages, Status rewrite
  innovation-agent/  FROZEN - see ADR-0010
  research-agent/    FROZEN - see ADR-0010
skills/            SKILL.md prompt bundles for the Worker/Mentor models
tests/             pytest suite; tests/fake_gowa/ is the Seam-1 test double
scripts/           operator scripts (add_bot_admin, worker model benchmark)
docs/              Spec, Plan, ADRs, Wiki Format, Operations, Architecture
main.py            composition root - wires agents into the Scheduler,
                   the Chat Agent into the Gateway, runs the app + tick loop
```

`AGENTS.md` has the module-boundary rules (each package's `interface.py`
is its only public surface).

## Running it

### Tests (no external services needed)

```sh
uv sync
uv run pytest -q
uv run ruff check .
uv run mypy shared
uv run mypy agents/wa_agent
uv run mypy agents/project_agent
uv run mypy main.py
```

Everything in `tests/` runs against `tests/fake_gowa/` (a gowa test
double) and `LocalDirClient` (a directory standing in for Lapis) -
Testing Decisions' Seam 1. No API keys, no live WhatsApp, no live wiki.

### The real thing

1. `cp .env.example .env` and fill in `GOWA_BASIC_AUTH`,
   `GOWA_WEBHOOK_SECRET` (any random string), and `MODELS_API_KEY` (an
   OpenRouter key or any OpenAI-compatible provider) once you have one.
   No Jev access? Leave `JEV_BASE_URL` empty - `main.py` falls back to
   `WorkerBackedDecisionModel`, a real (if slower/pricier) implementation
   that answers every Decision Model question with the Worker model
   instead. Everything else has a sane default - see `.env.example`.
2. `docker compose up -d --build`. This runs `main.py`, not raw
   `uvicorn` - it wires the batch cutter, Chat Agent, Project Agent and
   lifecycle jobs into the Scheduler and starts a tick loop, in addition
   to serving the FastAPI app.
3. Scan the WhatsApp QR at `http://<host>:3000/app/login` with the
   dedicated number.
4. `curl http://<host>:8000/healthz` should return `{"ok": true}`; check
   gowa/Lapis reachability separately with `check_gowa_connection()` /
   `check_vault_connection()` (see [`docs/Operations.md`](docs/Operations.md#health-checks)).
5. Bootstrap yourself as a Bot Admin - `bot_admins` starts empty, and
   every command needs a row there:
   `docker compose exec watcher uv run python scripts/add_bot_admin.py "<your-wa-jid>"`.
6. Message an allowlisted-to-be group with `/setup other` to confirm the
   webhook path, then `/setup project <Title>` for a real project - see
   [`docs/Operations.md`](docs/Operations.md) for the full deploy/
   re-login/rotation/backup runbook.

### Worker model benchmark (Phase 6)

```sh
uv run python scripts/run_worker_benchmark.py
```

Needs `MODELS_API_KEY` set for real - the shipped eval set
(`tests/fixtures/eval/worker_model_eval.json`) is a small synthetic
placeholder, not the Spec's real recorded-conversation set. Record the
result in [`docs/adr/0011-worker-model-choice-TEMPLATE.md`](docs/adr/0011-worker-model-choice-TEMPLATE.md)
once you've read the outputs yourself.

## Frozen prototypes

`agents/innovation-agent/` (idea generation) and `agents/research-agent/`
(arXiv/HN/HF digests) are real, working pre-pivot prototypes - not
scratch code - kept for a post-v1 decision on whether to resume them.
Don't extend or "clean up" either as a side effect of v1 work; see
[ADR-0010](docs/adr/0010-freeze-innovation-research-for-v1.md).
