---
type: note
title: Operations — deploy, re-login, rotation, backup/restore
status: draft
tags:
  - watcher
  - ops
---
# Operations

For the team running Watcher day to day, not for contributors reading
the code. See [[Spec - Watcher v1]] for what the system does and
`AGENTS.md` for the module boundaries if you're changing code.

## Deploy

1. Copy `.env.example` to `.env` and fill in `GOWA_BASIC_AUTH`,
   `GOWA_WEBHOOK_SECRET` (a random string - reused by gowa and watcher,
   see `docker-compose.yml`), and `MODELS_API_KEY` once you have one.
   Leave `GOWA_DEVICE_ID` as the example's `watcher` for now (step 3
   below creates a device with that id) - or pick your own and use it
   consistently in step 3.
2. `docker compose up -d --build`.
3. Create a gowa device slot and log in - required even for a single
   number, gowa's multi-device API has no implicit default device until
   one exists:
   ```sh
   curl -u <user>:<pass> -X POST http://<host>:3000/devices -d '{"device_id": "watcher"}'
   curl -u <user>:<pass> http://<host>:3000/devices/watcher/login
   ```
   The second call returns a `qr_link` - open it in a browser (it
   expires in 30s; re-run the same command for a fresh one) and scan it
   with the dedicated WhatsApp number (ADR-0001: the number is
   disposable, never a personal one). If you changed `GOWA_DEVICE_ID`
   away from `watcher`, `docker compose up -d` again to pick it up.
4. Confirm `GET http://<host>:8000/healthz` returns `{"ok": true}`.
5. Bootstrap yourself as a Bot Admin - there is no other way in on a
   fresh database:
   ```sh
   docker compose exec watcher uv run python scripts/add_bot_admin.py "<your-wa-jid>"
   ```
   You need your own WhatsApp JID as gowa reports it (e.g.
   `919876543210@s.whatsapp.net`) - send any message to a group the bot
   is in and read the `sender` field back out of `messages_buffer` (or
   the container logs) if you don't already know it.
6. Message an allowlisted or new group with `/setup other` to confirm
   the webhook path end to end, then `/setup project <Title>` for a
   real project.
7. Optional but recommended: `/setup logs <Title>` on a group you want
   the bot's own output in - job failures, ingest warnings, and the
   per-run cost report all go there. That channel is never itself
   ingested into threads (ADR-0013).
8. Observability comes up with the stack: Grafana on
   `http://<host>:3001`, Phoenix (traces) on `http://<host>:6006`. See
   "Observability" below.

Nothing here needs a rebuild for config-only changes - `.env` is read at
process start, so `docker compose up -d` after editing it is enough.

## Bot Admin commands (any allowlisted-or-not group, Bot Admin only)

- `/setup [project|event|coordis|exes|research|all|other|logs] [<Title>]`
  - register the group this is sent from as a watched channel.
  `coordis`/`exes`/`research`/`all`/`logs` are singletons (a second
  `/setup` of the same kind is refused). `logs` is where all debug
  logging and error reporting goes (job-failure alerts, and any future
  debug output) - never coordis, which is reserved for Proposals a Bot
  Admin needs to act on with a 👍. Set it up once with `/setup logs`
  from whatever group you want the bot's noise in.

  **Project and event channels** (`/setup project <Title>`,
  `/setup event <Title>`) are linked to `projects/<slug>.md` or
  `events/<slug>.md`. If that page already exists the channel is simply
  linked to it. If not, the bot asks three questions in the group - the
  lead (a name, an @tag, or "me"), the brief, and the timeline - and
  creates the page from the answers, then watches the channel. Answer
  with plain messages from the admin who ran `/setup` (no quoting
  needed; anyone else's messages are ignored until it's done). Say
  `skip` to leave the brief or timeline blank. The timeline is kept at
  the end of the page's `## Brief`; a lead that matches an ARIES member
  is written as that member's title, and one it can't place becomes
  `[[Unassigned]]` - never a phone number. Running `/setup` again in the
  group abandons a dialogue that was left half-finished.

  **Chat history.** Once a channel is watched (any kind except `logs`),
  `/setup` reads its last `SETUP_HISTORY_COUNT` (default 100) stored
  messages from gowa and queues a first ingest of them, which the bot
  reports on its reply (`Read 87 message(s) from the chat history;
  ingesting them now.`). That ingest is the `backfill_ingest` job: like
  `/ingest` it ignores the batch thresholds, but it keeps cutting
  batches until nothing is left, and it shares `ingest_tick`'s lock. It
  starts within one 30 s scheduler poll, and its cost report goes to
  the logs channel like any other run. History is best-effort: gowa
  only has what the linked device has seen, and if it can't be read the
  reply says so and only new messages are ingested. Re-running `/setup`
  never duplicates messages already buffered.
- `/channels` - list every watched channel with its jid, kind, and
  initiative - the jid is what `/unwatch <jid>` needs.
- `/unwatch` - stop watching the channel this is sent from.
  `/unwatch <jid>` - stop watching a *different* channel by jid (from
  `/channels`), without needing to be a member of it.
- `/status` - kind/initiative/cursor for the channel this is sent from.
- `/ingest` - for Bot Admins, immediately drain unprocessed messages in
  the channel where the command is sent, ignoring scheduled batch
  readiness thresholds. It uses the same `ingest_tick` lock as scheduled
  ingestion and backfill; if any ingestion already holds that lock, it
  reports that ingestion is running. The normal batch size and the
  pipeline's drain safety limit still apply.
- `/health` - gowa/vault/model connectivity, admin/channel/proposal
  counts, and the outcome of each scheduled job's last run
  (`ingest_tick`, `lifecycle_tick`, `project_agent_tick`) - check this
  proactively any time, from your phone.
- `/setup-members` - list this group's WhatsApp members, fuzzy-match
  each display name against the ARIES member roster, and link the
  confident ones. Everyone else is printed (with the roster's titles)
  so you can assign them yourself.
- `/link @member [[Member Title]]` - link the person you @mention in
  that message. A typed JID is ignored; the mention has to be a real
  WhatsApp mention.
- `/toggle-agent` - switch chat replies on or off. Off until the first
  toggle, including after a fresh database. Ingest and the other
  commands keep working either way.
- `/help` - list the commands above and whether the chat agent is on.

## Finding out about a failed scheduled job

Two ways, one proactive and one you check yourself:

1. **Pushed automatically**: when a scheduled job (`ingest_tick`,
   `lifecycle_tick`, `sunday_nudge_tick`, `project_agent_tick`) fails
   and exhausts its retries, `main.py`'s `_notify_job_failure` posts a
   `⚠️ <job_name> failed: <error>` message to the **logs** channel (the
   one `/setup logs` creates - never coordis). If no logs channel is
   set up yet, nothing gets pushed - `docker compose logs watcher`
   still has it (`logger.warning`), and it's still recorded in the run
   ledger either way.
2. **Check yourself**: `/health` reports the outcome, time, and error
   (if any) of each scheduled job's most recent run - useful right
   after deploying, or if you're not sure a logs channel exists yet.

## Re-login after a ban

The number is disposable by design (ADR-0001 consequence: "losing the
number costs a re-login, not data" - Spec user story 48). If gowa's
session drops or the number gets banned:

1. `docker compose restart gowa` (or `up -d` if the container itself
   needs replacing).
2. Re-fetch the QR for the same device id and scan it with the same or
   a new SIM: `curl -u <user>:<pass> http://<host>:3000/devices/watcher/login`.
3. Nothing in `watcher`'s own database needs touching - `channels`,
   `messages_buffer`, threads on the wiki, etc. are all keyed by JIDs
   gowa gives you again on reconnect, not by the underlying phone
   number.
4. If you swapped to a genuinely new number, gowa will report a new
   device/JID; existing channel JIDs (group JIDs) don't change when the
   *bot's* number changes, so no channel re-setup is needed either.

## Rotating the webhook secret

`shared/gateway/interface.py`'s `verify_signature` accepts both
`GOWA_WEBHOOK_SECRET` and `GOWA_WEBHOOK_SECRET_PREVIOUS` at once, so you
can rotate without a moment of dropped webhooks:

1. Set `GOWA_WEBHOOK_SECRET_PREVIOUS` to the *current* value of
   `GOWA_WEBHOOK_SECRET` in `.env`.
2. Set `GOWA_WEBHOOK_SECRET` to a new random value.
3. `docker compose up -d` (recreates `watcher` with both values live).
4. Update gowa's own webhook secret config to the new value and restart
   `gowa`.
5. Once you've confirmed webhooks are flowing again, remove
   `GOWA_WEBHOOK_SECRET_PREVIOUS` and redeploy once more to stop
   accepting the old one.

## CMS token handling

`CMS_TOKEN` (and `MODELS_API_KEY`, `GOWA_BASIC_AUTH`) live only in
`.env`, never in the wiki, never in a commit. `shared/cms/interface.py`'s
`CmsClient` sends it as a Bearer token and caches lookups for
`cache_ttl_s` (default 60s) so a compromised or expired token fails
fast rather than being retried silently. If a token leaks, rotate it at
the CMS side first, then update `.env` and redeploy - there's no local
copy to also scrub.

## PII in the wiki

**Since ADR-0012, PII is deliberately *not* stripped.** The wiki is
internal to the club, so a phone number a member actually typed stays in
the summary, and sender jids go into the ingestion prompts (they
disambiguate two people with the same display name). What pages render
is *people by name*: `[[Member]]` when the sender is linked in the
members registry, their WhatsApp display name otherwise, and the raw jid
only when gowa gave no name at all.

`lint_text()` no longer reports phone/email-shaped strings.
`shared/wiki/lint.py` still defines `contains_pii()` and `redact_pii()`
for any caller that wants an opt-in scrub - nothing calls them today.

Traces carry more: with `OTEL_INCLUDE_CONTENT=true` (the default) the
spans exported to Phoenix include prompts and message text. Phoenix is
self-hosted alongside the app, so that content does not leave the
deployment, but set `OTEL_INCLUDE_CONTENT=false` if you want spans
without message bodies.

## Restore from backup

Two independent things to back up:

- **The wiki** (Lapis vault): whatever backup mechanism Lapis itself
  provides for the vault - this repo's `LocalDirClient`/`LapisClient`
  don't add their own backup, they're just the read/write path.
- **`watcher.db`** (SQLite, the volume mounted in `docker-compose.yml`):
  copy the file while the container is stopped (or use
  `sqlite3 watcher.db ".backup backup.db"` for a live copy). It holds
  `messages_buffer`, `channels`, `proposals`, `members_registry`, the
  scheduler's `jobs`/`runs` ledger, and `model_calls` - none of it is
  reconstructable from the wiki alone (the wiki is *derived from* this
  plus model output, not the reverse, except human edits inside grammar
  and `state: ended` - Spec, Shape).

To restore: stop the containers, replace `watcher.db` with the backup,
restart. The wiki restore (if needed) is independent and happens on the
Lapis side.

## Health checks

`GET /healthz` only says the watcher process itself is up - it doesn't
tell you gowa or the wiki are reachable. Check those independently from
the command line:

```sh
make check-gowa    # or: uv run python scripts/check_gowa.py
make check-lapis   # or: uv run python scripts/check_lapis.py
```

Both print `{"ok": true/false, ...}` and exit 1 on failure -
`check-lapis` also prints which backend it resolved to (`LapisClient` if
`LAPIS_TOKEN` is set, else the local-directory fallback), so a script
can tell "not configured" from "configured but unreachable." Or call
the underlying functions directly from Python:

```python
from shared.gateway.interface import check_gowa_connection
from shared.wiki.interface import check_vault_connection, default_vault_client

await check_gowa_connection()                          # {"ok": bool, "data"/"error": ...}
await check_vault_connection(default_vault_client())    # {"ok": bool, "path_count"/"error": ...}
```

Both are plain async functions (no HTTP dependency), so a monitoring
script, a cron job, or a future `/healthz/gowa` + `/healthz/vault`
endpoint can call them directly. They never raise - a connection
failure comes back as `{"ok": false, "error": "..."}`, not an exception,
so a health-check loop can call them unconditionally.

## Recurring jobs

`main.py` (what the Dockerfile actually runs, not raw `uvicorn`) wires
six Scheduler jobs on startup: `ingest_tick` (batch cutting, every 2
min), `backfill_ingest` (the first full
ingest of a channel `/setup` just imported history for), `project_agent_tick` (works through
`inbox/project_agent.md`, every 2 min - or on the next 30 s poll when a
triggering Inbox Item is posted, ADR-0014), `lifecycle_tick`
(stale/ended handling, daily), `sunday_nudge_tick` (daily, only acts on
Sundays) and `expire_proposals_tick` (hourly). It also registers the
Chat Agent as a real-time message hook and Proposal confirmation as a
reaction hook, so mentions/replies and 👍s are handled immediately
rather than waiting for the next batch. Bot Admins can also ask the Chat
Agent to ingest a watched channel on demand; the tool runs the same
pipeline under the shared ingestion lock and accepts a channel JID,
channel title, or initiative name. `/health` lists the last run of
every registered job.

An agent's Inbox is a wiki page you can read (and add to: a line typed
under `## Pending` is picked up as a request). Items move to `## Done`
only after they were applied, so a failed apply shows up as an item
that stays pending. Check `GET /api/scheduler/due-jobs`
to see what's pending; `shared.scheduler.interface.ledger_tail(name)`
for a job's recent run history (success/failure, errors).

## Observability (ADR-0013)

Three things to look at, in increasing order of detail.

**The logs channel.** After every ingest run that processed at least one
message, the bot posts a report: total time and cost, a line per channel
with its duration and thread counts, and the token/cost split between
classification (assigning messages to threads) and summarisation
(rewriting a thread's title/summary/items). Runs that found nothing
stay silent - the tick fires every 2 minutes, so reporting
unconditionally would post ~720 messages/day. `OBS_REPORT_MIN_MESSAGES`
is the threshold.

**Grafana** on `http://<host>:3001` (3000 is gowa's). The datasource and
the "Watcher - cost & ingestion" dashboard are provisioned from
`shared/observability/grafana/provisioning/`, so there is nothing to
click: bring the stack up and the dashboard has data. The panels that
matter for tuning are **cost per message** and **cost per thread** -
change a prompt, watch those two.

Grafana reads `watcher.db` directly via the `frser-sqlite-datasource`
plugin. The data volume is mounted read-**write** on purpose: the app
keeps the database in WAL mode (so Grafana's reads never block the
app's writes) and SQLite must be able to create `-wal`/`-shm` sidecars,
which fails on a read-only mount. Grafana only ever issues SELECTs.

**Phoenix** on `http://<host>:6006` - per-call traces with prompts,
replies, token counts and latency, nested under each agent run. The app
exports OpenTelemetry GenAI spans there (`OTEL_ENDPOINT`). If Phoenix is
down the app logs a warning and carries on; tracing is never a hard
dependency. Point `OTEL_ENDPOINT` at any other OTLP/HTTP collector to
switch backends, or set `OTEL_ENABLED=false` to turn it off.

The tables behind all this, in the same SQLite file as everything else:
`obs_ingest_runs`, `obs_ingest_channel_runs` (per-channel duration and
per-phase tokens/cost), `obs_vault_ops` (every wiki write/delete, with
conflicts), and `model_calls` (per call: phase, channel, tokens, cost,
`cost_source`, latency, outcome).

`cost_source` says how much to trust a cost: `reported` (the provider
returned it - OpenRouter always does), `computed` (priced from tokens by
genai-prices), `estimated` (a configured flat rate, currently only Jev,
which bills per decision and reports no tokens), or `unknown` (no price
available - stored as NULL, never as zero).

On a database that predates ADR-0013, run
`uv run python scripts/backfill_observability.py` (dry run; `--yes` to
apply) to mark old unattributable rows `phase='legacy'` - including the
~550 bogus zero-token `decision/jev` rows the old
`decide_with_fallback` wrote on every call.

## HTTP adapter

`shared/http_adapter.py` exposes a subset of the package interfaces as
JSON endpoints under `/api/*` (`/api/gateway/send`,
`/api/gateway/messages/{id}`, `/api/gateway/channels`,
`/api/wiki/lint`, `/api/scheduler/due-jobs`). v1 has no out-of-process
caller that uses these - they exist so the contract is exercised end to
end (Spec, Shape) and as the seam a later MCP exposure would sit behind.
