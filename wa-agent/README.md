# WA Agent (TheWatcher)

The WhatsApp agent for ARIES. Same shape as `innovation-agent`: everything
talks to memory through one interface, the LLM step returns structured
output via a forced tool call, and the whole pipeline runs against a mock
before gowa / Lapis / an LLM key exist.

## What it does

Three jobs, all flowing through one place:

1. **Chat ingestion** — WhatsApp (project + TLDR/announcement groups) →
   classified signals → written to the wiki in **two layers**:
   - the **durable record** (Lapis is ARIES's knowledge base — history &
     info: the research corpus, idea record, project pages, events), AND
   - the owning agent's **input page** — a "what's new for your next run"
     queue layered on top, which that agent reads and clears when it runs.

   So a research paper found in chat becomes a permanent entry in
   `research/` *and* a line on `research/_input.md`; an idea lands in
   `ideas/` *and* on `innovation/_input.md`; a project update appends to the
   project's page. Trigger order: **Project agent before Research agent**.
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
  `search`); `HttpLapisClient` (real) + `FakeLapisClient` (tests).
- `lapis_adapter.py` — **real `LapisAdapter(WAMemory)`** over Lapis. The
  system's single wiki adapter; the other agents' methods fold in here.
  Writes the **durable record** (`LAYOUT`: research corpus, ideas, projects,
  events, tasks) *and* the per-agent **input queues** (`INPUT_PAGES`), with
  `read_agent_input` / `consume_agent_input` for the consuming agents. Both
  the folder/frontmatter convention and the input-page paths are the
  one-place edits for the real vault schema.
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

Tests / demo:
- `demo.py` — full inbound+outbound flow on mocks.
- `test_wa_agent.py` — 13 tests (preprocessing, routing, pipeline).
- `test_gowa.py` — 11 tests (signature, webhook parsing, buffer, rate limit, reminders).
- `test_lapis.py` — 13 tests (durable record + input queues, consume, reminder reads, notifications).
- `test_dispatcher.py` — 6 tests (trigger records, dedup, handled, pipeline).
- `test_group_context.py` — 8 tests (registry, cadence, per-group buffers, routing).

## Run it

```bash
python demo.py             # mocks, no key, no network
python test_wa_agent.py    # 13/13
python test_gowa.py        # 11/11
python test_lapis.py       # 13/13
python test_dispatcher.py  # 6/6
python test_group_context.py  # 8/8
```

Real classifier (Kimi K2.6):
```bash
pip install openai
export OPENROUTER_API_KEY=...          # Windows: set OPENROUTER_API_KEY=...
python demo.py
```

Live against WhatsApp + the real Lapis wiki (needs gowa running + paired):
```bash
pip install flask openai
export GOWA_BASE_URL=http://localhost:3000
export WA_WEBHOOK_SECRET=<same secret gowa is configured with>
export WA_ALLOWLIST=<groupJID1>,<groupJID2>
export LAPIS_BASE_URL=https://lapis.dvenom.in
export LAPIS_VAULT_ID=6adc07d5-b530-462f-be6d-cc399288bb78
export LAPIS_TOKEN=<device bearer token from Lapis>
python run_live.py         # POST target for gowa's WHATSAPP_WEBHOOK
```
Omit the `LAPIS_*` vars to run against MockWAMemory (smoke-test gowa alone).

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

- **Confirm vault schema** — `LapisAdapter` works, but its `LAYOUT` folders
  and frontmatter field names are our convention. Confirm against the real
  ARIES vault (folder names, event/task/project frontmatter) and the exact
  `/manifest` JSON shape — both are one-place edits (`LAYOUT` / `list_files`).
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
