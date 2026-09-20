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

Nothing here needs a rebuild for config-only changes - `.env` is read at
process start, so `docker compose up -d` after editing it is enough.

## Bot Admin commands (any allowlisted-or-not group, Bot Admin only)

- `/setup [project|event|coordis|exes|research|all|other] [<Title>]` -
  register the group this is sent from as a watched channel.
  `project`/`event` open a lead/brief/timeline dialogue if the
  initiative page doesn't exist yet; `coordis`/`exes`/`research`/`all`
  are singletons (a second `/setup` of the same kind is refused).
- `/channels` - list every watched channel with its jid, kind, and
  initiative - the jid is what `/unwatch <jid>` needs.
- `/unwatch` - stop watching the channel this is sent from.
  `/unwatch <jid>` - stop watching a *different* channel by jid (from
  `/channels`), without needing to be a member of it.
- `/status` - kind/initiative/cursor for the channel this is sent from.
- `/ingest` - run the batch-cutting ingestion job (`ingest_tick`) right
  now instead of waiting for its next 2-minute tick - useful right
  after `/setup` or while testing against a live channel.
- `/health` - gowa/vault/model connectivity, admin/channel/proposal
  counts, and the outcome of each scheduled job's last run
  (`ingest_tick`, `lifecycle_tick`, `project_agent_tick`) - check this
  proactively any time, from your phone.
- `/link <wa-jid> [[Member Title]]` - manually link a WhatsApp identity
  to a Members Registry page, bypassing fuzzy-match Proposals.

## Finding out about a failed scheduled job

Two ways, one proactive and one you check yourself:

1. **Pushed automatically**: when a scheduled job (`ingest_tick`,
   `lifecycle_tick`, `sunday_nudge_tick`, `project_agent_tick`) fails
   and exhausts its retries, `main.py`'s `_notify_job_failure` posts a
   `⚠️ <job_name> failed: <error>` message to the coordis channel (the
   one Bot Admin channel `/setup coordis` creates). If no coordis
   channel is set up yet, nothing gets pushed - `docker compose logs
   watcher` still has it (`logger.warning`), and it's still recorded
   in the run ledger either way.
2. **Check yourself**: `/health` reports the outcome, time, and error
   (if any) of each scheduled job's most recent run - useful right
   after deploying, or if you're not sure a coordis channel exists yet.

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

`shared/wiki/lint.py`'s `lint_text()` flags phone- and email-shaped
strings in any page section (ADR-0009) - this is exercised by
`tests/test_lint_and_derive.py` in CI on every push, so a regression
that stops catching PII fails CI, not just a manual run. Running lint
against the *live* vault (not just fixtures) is the derived-regeneration
job's responsibility once wired to the Scheduler (Phase 3's job wiring,
Plan Phase 1) - `meta/lint.md` is where a human should look for the
current state of the real vault.

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
four Scheduler jobs on startup: `ingest_tick` (batch cutting, every 2
min), `project_agent_tick` (Inbox consumption, every 2 min),
`lifecycle_tick` (stale/ended handling, daily), `sunday_nudge_tick`
(daily, only acts on Sundays). It also registers the Chat Agent as a
real-time message hook, so mentions/replies get answered immediately
rather than waiting for the next batch. Check `GET /api/scheduler/due-jobs`
to see what's pending; `shared.scheduler.interface.ledger_tail(name)`
for a job's recent run history (success/failure, errors).

## HTTP adapter

`shared/http_adapter.py` exposes a subset of the package interfaces as
JSON endpoints under `/api/*` (`/api/gateway/send`,
`/api/gateway/messages/{id}`, `/api/gateway/channels`,
`/api/wiki/lint`, `/api/scheduler/due-jobs`). v1 has no out-of-process
caller that uses these - they exist so the contract is exercised end to
end (Spec, Shape) and as the seam a later MCP exposure would sit behind.
