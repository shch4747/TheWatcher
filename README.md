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
make check          # lint + typecheck + test - same as CI
# or individually:
make test           # uv run pytest -q
make lint           # uv run ruff check .
make typecheck      # uv run mypy shared / agents/* / main.py
```

Everything in `tests/` runs against `tests/fake_gowa/` (a gowa test
double) and `LocalDirClient` (a directory standing in for Lapis) -
Testing Decisions' Seam 1. No API keys, no live WhatsApp, no live wiki.

### Testing the gowa/Lapis connections separately

```sh
make check-gowa     # uv run python scripts/check_gowa.py
make check-lapis    # uv run python scripts/check_lapis.py
```

Both read your `.env` and print `{"ok": true/false, ...}` - exit code 1
on failure, so they're script/CI-friendly. `check-lapis` reports which
backend it's using (`LapisClient` if `LAPIS_TOKEN` is set, otherwise the
local-directory fallback at `VAULT_ROOT`) so you can tell "not
configured" from "configured but unreachable."

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
3. Create a gowa device slot and log in - required even for one number,
   gowa's multi-device API has no implicit default until one exists:
   ```sh
   curl -u <user>:<pass> -X POST http://<host>:3000/devices -d '{"device_id": "watcher"}'
   curl -u <user>:<pass> http://<host>:3000/devices/watcher/login
   ```
   Open the returned `qr_link` and scan with the dedicated number (QR
   expires in 30s - re-run the second command for a fresh one).
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
