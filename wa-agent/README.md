# WA Agent (TheWatcher)

The WhatsApp agent for ARIES. Same shape as `innovation-agent`: everything
talks to memory through one interface, the LLM step returns structured
output via a forced tool call, and the whole pipeline runs against a mock
before gowa / Lapis / an LLM key exist.

## What it does

Three jobs, all flowing through one place:

1. **Chat ingestion** — WhatsApp (project + TLDR/announcement groups) →
   classified signals → written to the wiki (Lapis is ARIES's knowledge
   base — history, projects, research, events — not just a buffer). Each
   signal lands where the owning agent expects it:
   - research topics/papers → **`WhatsApp/ResearchDigest.md`** (the exact
     page the Research agent hashes and reacts to — writing here *is* the
     research trigger; confirmed against `research-agent/src/config.ts`),
   - ideas → **`Ideas/Inbox.md`** (Innovation agent's inbox),
   - project updates → the **project's page** under `Projects/`,
   - events / tasks / decisions → the wiki.

   Trigger order: **Project agent before Research agent**.
2. **Notification gateway (group-aware)** — other agents
   `enqueue_notification(...)` with a *logical* audience (`project:<ref>`,
   `announce`); the WA agent resolves it to the right group JID(s) via the
   **GroupRegistry**, summarises the output **in that group's context**, and
   sends via gowa. Producers never touch gowa — one throttle/format point.
3. **Reminders** — reads upcoming events / pending tasks / stale projects
   from the **wiki** (GitHub feeds the wiki, not this agent) and nudges the
   right group.

**Group-based context** (`group_context.py`): each group has a name, kind
(project/announce), the project(s) it's tied to, and its own ingest cadence
(N messages / X hours) — config in the wiki at `meta/groups.md`. This drives
per-group buffering on ingest and project→group routing on outbound, plus a
rolling per-group summary used to phrase messages.

## Files

Core (deterministic, no network):
- `schemas.py` — dataclasses + `SIGNAL_TO_AGENT` (the routing rule).
- `preprocessing.py` — dedup → allowlist → normalize → pre-filter → threads.
- `media.py` — `MediaProcessor` (OCR/voice/doc), `StubMediaProcessor` for now.
- `router.py` — `Signal` → `RoutingDecision`.
- `memory_interface.py` — `WAMemory` + `Dispatcher` interfaces, `MockWAMemory`
  / `MockDispatcher` for tests.
- `lapis_client.py` — client over Lapis's REST API (`manifest`, `files/*`,
  `search`); `HttpLapisClient` (real, auth via `LAPIS_BEARER_TOKEN`) +
  `FakeLapisClient` (tests). Same env-var names the research-agent uses, so
  one `.env` serves both.
- `lapis_adapter.py` — **real `LapisAdapter(WAMemory)`** over Lapis. The
  system's single wiki adapter; the other agents' methods fold in here. Page
  paths (`LAYOUT` + `INPUT_PAGES`) are aligned to the real vault
  (`Projects/`, `WhatsApp/ResearchDigest.md`, …) and are the one-place edit
  for any schema change.
- `mdfront.py` — markdown+YAML frontmatter parse/dump.
- `group_context.py` — `GroupContext` + `GroupRegistry`: allowlist, per-group
  cadence, project→group resolution, announce groups (config from `meta/groups.md`).
- `dispatcher.py` — how the WA agent triggers the other agents:
  `LoggingDispatcher` (dev) and `QueueDispatcher` (writes trigger records to
  `inbox/triggers/<agent>/` in the wiki for the target agent to poll, with
  per-project dedup).
- `pipeline.py` — `ingest_batch` (inbound) + `flush_notifications` (outbound).
- `reminders.py` — build/enqueue reminder notifications from wiki reads.

LLM + WhatsApp:
- `llm_client.py` — `classify_thread`: forced tool call → `Signal`s, model =
  **Kimi K2.6** (`moonshotai/kimi-k2.6` via OpenRouter), with a heuristic
  stub so it runs keyless.
- `gowa_client.py` — rate-limited client over gowa's REST API (`/send/message`).
- `webhook.py` — `verify_signature`, `parse_webhook_event`, `InboxBuffer`
  (n-messages / x-seconds trigger), and an optional Flask receiver.
- `run_live.py` — how it's wired for real operation.

Demo:
- `demo.py` — full inbound+outbound flow on mocks.

Tests + connectivity checks live in **`testing/`** — see
`testing/test_readme.md` for the full guide. Quick version:
```bash
python demo.py                       # mocks, no key, no network
python testing/test_wa_agent.py      # 13/13
python testing/test_gowa.py          # 11/11
python testing/test_lapis.py         # 13/13
python testing/test_dispatcher.py    # 6/6
python testing/test_group_context.py # 8/8
python testing/check_lapis.py        # live Lapis connectivity (needs LAPIS_* env)
python testing/check_gowa.py         # live WhatsApp login + group JIDs (needs gowa)
```

## Implementing / deploying the WA agent

Bring the pieces up in this order — each is independently checkable.

**1. Confirm the two live dependencies first** (see `testing/test_readme.md`):
- Lapis: set `LAPIS_*` env and run `testing/check_lapis.py`.
- WhatsApp: stand up gowa, scan the QR, run `testing/check_gowa.py`.

**2. Install deps** on the host that will run the agent (the Pi/VPS):
```bash
pip install flask openai
```

**3. Set the environment** (put these in a `.env` or your process manager):
```bash
# WhatsApp transport
GOWA_BASE_URL=http://localhost:3000
GOWA_BASIC_AUTH=admin:yourpassword
WA_WEBHOOK_SECRET=<same secret gowa is started with>
# classifier
OPENROUTER_API_KEY=<key>            # model defaults to moonshotai/kimi-k2.6
# wiki (same names as research-agent)
LAPIS_BASE_URL=https://lapis.dvenom.in
LAPIS_VAULT_ID=6adc07d5-b530-462f-be6d-cc399288bb78
LAPIS_BEARER_TOKEN=<device bearer token>
# groups: either meta/groups.md in the vault, or a plain fallback list
WA_ALLOWLIST=<groupJID1>,<groupJID2>
```
Omit the `LAPIS_*` vars to run against `MockWAMemory` (smoke-test gowa alone).

**4. Configure the group registry** — create `meta/groups.md` in the vault
(see `group_context.py` for the frontmatter shape): each group's JID, kind
(`project`/`announce`), the project(s) it maps to, and its ingest cadence.
`run_live.py` loads it automatically; without it, `WA_ALLOWLIST` is used
with default cadence.

**5. Point gowa at the receiver** and run it:
```bash
# gowa started with:  --webhook="http://<host>:5000/webhook" --webhook-secret="$WA_WEBHOOK_SECRET"
python run_live.py         # serves POST /webhook on :5000
```
`run_live.py` auto-selects `LapisAdapter` + `QueueDispatcher` when `LAPIS_*`
is set (else mock/logging). Per-group ingest already fires on cadence via
`GroupBuffers`.

**6. Still to add for a complete daemon:** a scheduler loop that calls
`pipeline.flush_notifications(registry)` (send queued agent outputs) and
`reminders.run_reminder_job(memory, default_audience="announce")` on their
own cadences. Deploy gowa in Docker with a persistent session volume +
restart-on-crash, and add a health check that alerts on session drop.

## gowa / WhatsApp setup notes

- gowa links to a **normal WhatsApp account on a dedicated number** (not a
  personal one) as a companion device — first pairing needs one QR scan
  (`GET /app/login`); after that it reconnects on its own.
- In groups it appears as a **normal participant** (no "bot" badge — that's
  only the official Business Cloud API). Set the account's name/photo to
  "ARIES Bot". A group **admin adds its number** to the group; add it only
  to allowlisted groups.
- Point gowa at this receiver: `WHATSAPP_WEBHOOK=http://<host>:5000/webhook`,
  `WHATSAPP_WEBHOOK_SECRET=<secret>`. For local dev, tunnel with
  cloudflared/ngrok or run gowa + receiver on the same box (e.g. the Pi).

## What's stubbed / next

- **Confirm remaining vault paths** — research (`WhatsApp/ResearchDigest.md`)
  and projects (`Projects/`) are confirmed against research-agent; events /
  tasks / ideas paths are still our convention. Run `testing/check_lapis.py`,
  read the printed file list, and adjust `LAYOUT` / `INPUT_PAGES` to match.
- **`StubMediaProcessor`** — real OCR (screenshots!), vision, optional Whisper.
- **Wire the agents to their trigger queue** — `QueueDispatcher` now drops
  trigger records in `inbox/triggers/<agent>/`; each agent (research /
  innovation / project) needs to poll its folder and act (`pending_triggers`
  / `mark_handled` are provided).
- **Scheduler** — the remaining runtime piece: run `ingest` per-group cadence
  (done via `GroupBuffers`), `flush_notifications(registry)` on a short loop,
  and `run_reminder_job(...)` daily.
- **Populate `meta/groups.md`** — the group→project map + per-group cadence.
- **Outbound LLM summariser** — real Kimi summaries per group are wired
  (`summarise_notification`); tune the prompt once live.
- **Webhook payload field-drift** — `parse_webhook_event` is defensive but
  written against gowa v8's documented shape; confirm against your build's
  actual payload once gowa is live (one function to adjust).
- **Classifier hardening** — parse `message.content` fallback when the model
  skips the tool call (same fix noted in innovation-agent).
- **Cross-process write safety** — the in-process lock guards agent-vs-agent
  on one process; add optimistic concurrency (re-check manifest hash before
  PUT) when multiple agent processes write the same note.
