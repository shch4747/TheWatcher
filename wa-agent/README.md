# WA Agent (TheWatcher)

The WhatsApp agent for ARIES (AI/ML club, IIT Delhi). Part of a multi-agent
society where agents coordinate through a shared Lapis wiki. The WA agent is
the club's interface to WhatsApp — it ingests chat, writes structured
information to the wiki, triggers sibling agents, and sends outbound messages
on their behalf.

## Architecture

```
WhatsApp groups (via gowa webhook)
        │
        ▼
  ┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
  │ Preprocessing│────▶│ LLM Classify │────▶│  Route + Execute │
  │ (5 stages)  │     │ (Kimi K2.6)  │     │                 │
  └─────────────┘     └──────────────┘     └────┬───┬───┬────┘
                                                │   │   │
                    ┌───────────────────────────┘   │   └──────────────┐
                    ▼                               ▼                  ▼
             Wiki writeback                  Agent triggers      Notifications
             (Lapis vault)                   (dispatcher)        (outbound queue)
                    │                               │                  │
                    ▼                               ▼                  ▼
          Projects/, Events/,              inbox/triggers/     WhatsApp/output/
          ResearchDigest.md,               research/           <agent>.md (×3)
          Ideas/Inbox.md, ...              innovation/          ──▶ gowa ──▶ WA
                                           project/
```

## Three Jobs

1. **Chat ingestion** — WhatsApp messages from project, announcement, events,
   coordi, and exes groups → preprocessed → LLM-classified into typed signals
   → written to the wiki + trigger sibling agents:
   - Research papers/links → `WhatsApp/ResearchDigest.md` (Research agent's input)
   - Ideas, feedback → `Ideas/Inbox.md` (Innovation agent's input)
   - Project updates, tasks → `Projects/<name>.md` (Project agent's input)
   - Events, decisions, MoM, user pings → wiki archive
   - Trigger order: **Project agent before Research agent**

2. **Notification gateway** — each sibling agent writes to its own output file
   `WhatsApp/output/<agent>.md` (per-agent sub-paths eliminate cross-agent write
   contention). The WA agent merges all files on flush, resolves logical audiences
   (`project:dashboard`, `announce`, `events`, etc.) to concrete group JID(s) via
   the GroupRegistry, summarises in each group's context, and sends via gowa.
   See `docs/agent-output-guide.md` for the full integration spec.

3. **Reminders** — reads upcoming events / pending tasks / stale projects from
   the wiki and nudges the appropriate group. **Dedup via `ReminderState`**:
   events re-fire only at bracket transitions (3 days → 1 day → day-of); task
   digest once per day; project nudges once per day per project. State persisted
   to `WhatsApp/.state/reminder_state.json`.

## Signal Types and Routing

| Signal | Target Agent | Wiki Method | Notify? |
|--------|-------------|-------------|---------|
| `research_paper` | Research | `append_research_mention` | No |
| `project_idea` | Innovation | `record_idea` | No |
| `event_idea` | Innovation | `record_idea` | No |
| `feedback` | Innovation | `record_feedback` | No |
| `project_update` | Project | `append_project_update` | No |
| `task` | Project | `upsert_task` | No |
| `event_info` | — | `upsert_event` | Yes |
| `decision` | — | `write_chat_digest` | Yes |
| `announcement` | — | `write_chat_digest` | Yes |
| `mom` | — | `write_mom` | Yes |
| `user_ping` | — | `write_user_ping` | No |
| `question` | — | `write_chat_digest` | No |
| `noise` | — | — | No |

## Group Types

| Kind | Description | Preprocessing |
|------|------------|---------------|
| `project` | Project-specific groups (Dashboard, ML, etc.) | Normal |
| `announce` | ARIES TLDR / broadcast groups | Normal |
| `events` | ARIES Events group | Normal |
| `coordi` | General chat for coordinators — high casual spam | **Heavy** (stricter thresholds) |
| `exes` | General chat for executives — high casual spam | **Heavy** (stricter thresholds) |

Coordi and exes groups have aggressive noise filtering (expanded noise word
list, higher minimum word count) because they're general chat groups with a
very low signal-to-spam ratio. Config lives in `meta/groups.md` in the vault.

## Agentic Features

- **Group context on join** — when someone joins a group, the bot sends them a
  welcome summary of recent activity.
- **Rolling group context** — each group maintains a short summary of recent
  notable activity; this is passed to the LLM classifier and used to phrase
  outbound messages.
- **Feedback collection** — feedback signals (event/project feedback from chat)
  are archived and forwarded to the Innovation agent.
- **Minutes of Meeting** — MoM signals are archived under `WhatsApp/MoM/` and
  echoed back to the group.
- **User pings** — `@bot note this: ...` messages are written to the chat digest
  for manual Lapis updates.
- **Health monitoring** — `GET /health` endpoint reports gowa session status,
  Lapis connectivity, outbound queue depth, inbound buffer depth, and
  classification stats. Returns 200 (healthy) or 503 (degraded).

## Files

Core (deterministic, no network):

- `schemas.py` — dataclasses, `SignalType`, `SIGNAL_TO_AGENT` routing table.
- `preprocessing.py` — 5-stage pipeline: dedup → allowlist → normalize →
  pre-filter → threads. Group-aware: coordi/exes get heavier noise filtering.
- `media.py` — `MediaProcessor` interface + `StubMediaProcessor`.
- `router.py` — `Signal` → `RoutingDecision` (wiki method + agent target + notify flag).
- `memory_interface.py` — `WAMemory` + `Dispatcher` ABCs, mock implementations.
- `pipeline.py` — `WAPipeline`: `ingest_batch` (inbound) + `flush_notifications` (outbound).
- `group_context.py` — `GroupContext` + `GroupRegistry`: allowlist, per-group
  cadence, project→group resolution, noisy group detection.
- `mdfront.py` — markdown + YAML frontmatter parse/dump (works with or without PyYAML).

Wiki + dispatch:

- `lapis_client.py` — REST client for Lapis (`HttpLapisClient` + `FakeLapisClient`).
  Manifest-hash caching: file reads and listings are cached locally and
  invalidated on writes or when the vault's manifest hash changes; idle polls
  collapse to one cheap manifest call.
- `lapis_adapter.py` — `LapisAdapter(WAMemory)`: the real wiki backend. Page paths
  in `LAYOUT` + `INPUT_PAGES` are aligned to the ARIES vault. Outbound queue uses
  per-agent sub-paths under `WhatsApp/output/`.
- `dispatcher.py` — `QueueDispatcher` (writes trigger records to `inbox/triggers/<agent>/`
  for sibling agents to poll) + `LoggingDispatcher` (dev).
- `reminders.py` — build/enqueue reminders from wiki reads (events, tasks, stale projects).
  `ReminderState` tracks sent reminders with window-bracket dedup.

LLM + WhatsApp:

- `llm_client.py` — `classify_thread`: Kimi K2.6 via OpenRouter, with heuristic stub.
- `gowa_client.py` — rate-limited client for gowa's REST API.
- `webhook.py` — signature verification, event parsing, `GroupBuffers` (per-group
  batching with n-messages / age triggers), optional Flask receiver.
- `scheduler.py` — periodic scheduler: outbound flush (2min), reminders (1hr),
  ingest check (30s). Standalone or daemon thread mode.
- `run_live.py` — production wiring.

## Tests

All tests in `testing/`, runnable with `python` or `pytest`:

```bash
python demo.py                         # mocks, no key, no network
python testing/test_wa_agent.py        # 21 tests
python testing/test_gowa.py            # 11 tests
python testing/test_lapis.py           # 22 tests (incl. per-agent output, reminder dedup)
python testing/test_dispatcher.py      # 6 tests
python testing/test_group_context.py   # 14 tests
                                       # Total: 74 tests
python testing/check_lapis.py          # live Lapis connectivity (needs LAPIS_* env)
python testing/check_gowa.py           # live WhatsApp login + group JIDs (needs gowa)
```

## Deploying

**1. Confirm live dependencies** (see `testing/test_readme.md`):
- Lapis: set `LAPIS_*` env, run `testing/check_lapis.py`.
- WhatsApp: stand up gowa, scan QR, run `testing/check_gowa.py`.

**2. Install deps:**
```bash
pip install flask openai
```

**3. Set environment** (`.env` or process manager):
```bash
GOWA_BASE_URL=http://localhost:3000
GOWA_BASIC_AUTH=admin:yourpassword
WA_WEBHOOK_SECRET=<gowa webhook secret>
OPENROUTER_API_KEY=<key>
LAPIS_BASE_URL=https://lapis.dvenom.in
LAPIS_VAULT_ID=6adc07d5-b530-462f-be6d-cc399288bb78
LAPIS_BEARER_TOKEN=<device bearer token>
WA_ALLOWLIST=<groupJID1>,<groupJID2>    # fallback if meta/groups.md missing
```

**4. Configure groups** — create `meta/groups.md` in the vault with each group's
JID, kind, projects, and cadence (see `group_context.py` for the schema).

**5. Run:**
```bash
python run_live.py    # Flask webhook on :5000 + scheduler thread
```

## gowa / WhatsApp Setup

- gowa links to a **normal WhatsApp account on a dedicated number** as a
  companion device — first pairing needs one QR scan (`GET /app/login`);
  after that it reconnects automatically.
- In groups it appears as a normal participant (no "bot" badge). Set the
  account's name/photo to "ARIES Bot". A group admin adds its number to
  the group; add it only to allowlisted groups.
- Point gowa's webhook at the receiver:
  `--webhook="http://<host>:5000/webhook" --webhook-secret="$WA_WEBHOOK_SECRET"`

## What's Stubbed / Next

- **`StubMediaProcessor`** — real OCR (screenshots), vision, optional Whisper
  for voice notes.
- **Classifier hardening** — parse `message.content` fallback when the model
  skips the tool call.
- **Cross-process write safety** — per-agent output sub-paths eliminate the
  worst contention; add optimistic concurrency (conditional PUT with manifest
  hash) for project pages where multiple agents may still write.
- **Outbound LLM summariser** — real Kimi summaries per group are wired
  (`summarise_notification`); tune the prompt once live data flows.
- **Confirm remaining vault paths** — research and project paths are confirmed;
  events/tasks/ideas paths are still convention. Adjust `LAYOUT`/`INPUT_PAGES`
  after checking the live vault.
- **Parallelize LLM classification** — `ThreadPoolExecutor` for classify calls;
  5-10x throughput improvement during message surges.
- **Serialize pipeline access** — per-group lock or single ingest queue between
  the scheduler thread and webhook thread to prevent race conditions.
