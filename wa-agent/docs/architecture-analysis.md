# WA Agent Architecture Analysis

## Current Architecture Summary

The WA agent sits at the centre of a three-agent society (Research, Innovation, Project) that coordinates through a shared Lapis wiki. Inbound: gowa webhook → preprocessing (5 stages) → LLM classification (Kimi K2.6) → wiki writeback + agent triggers + outbound notifications. Outbound: agents write to `outputmessages.md` → WA agent polls, resolves audiences, summarises per-group, sends via gowa. Reminders read wiki state and enqueue notifications on the same outbound path.

---

## Bottlenecks

### 1. Single-file output queue (`outputmessages.md`)

**What:** Every agent (WA, Research, Innovation, Project) writes to one file. Each write is a read-modify-write cycle on the same Lapis path.

**Risk:** Under concurrent writes, the last writer wins — an agent that reads stale state will silently overwrite another agent's queued entry. The in-process `threading.Lock` in `LapisAdapter` only guards within a single Python process; cross-process (multiple agents) has no protection.

**Severity:** Medium-high once multiple agents are live.

**Mitigation options:**
- ~~Short-term: have each agent use a unique sub-path (`WhatsApp/output/<agent>.md`) and the WA agent merge them on flush. Eliminates cross-agent contention entirely.~~ **✅ IMPLEMENTED** — `lapis_adapter.py` now writes per-agent files under `WhatsApp/output/<agent>.md` and merges all files on flush. Legacy `outputmessages.md` still read for backward compatibility.
- Medium-term: Lapis optimistic concurrency — read the manifest hash, include it on PUT, retry on conflict (Lapis supports this via ETags; the client just doesn't use it yet).
- Long-term: a proper message queue (Redis stream, SQLite WAL) if throughput demands it.

### 2. LLM classification is the throughput ceiling

**What:** Every non-noise thread goes to Kimi K2.6 via OpenRouter. At ~2s per call (network + inference), a batch of 10 threads takes ~20s.

**Risk:** During a surge (club event ends, 50 messages arrive in 2 minutes across 5 groups), classification becomes the bottleneck. The scheduler's 30s ingest-check cadence means batches pile up.

**Mitigation options:**
- Classify threads in parallel (`asyncio.gather` or `ThreadPoolExecutor`) — the calls are independent.
- Raise the noise filter thresholds to keep fewer threads for the LLM (already done for coordi/exes; could add a "burst mode" that raises thresholds globally when batch size exceeds a threshold).
- Cache classification results by thread content hash — identical threads (re-sent on reconnect) skip the LLM.

### 3. Lapis as both datastore and message bus

**What:** Lapis is an Obsidian sync server, not a database. Every read/write is an HTTP round-trip; there's no query language, no transactions, no pub/sub.

**Risk:** The trigger polling pattern (agent lists `inbox/triggers/<agent>/` every N seconds) generates O(agents × poll_rate) list calls even when nothing is pending. Reminder job reads every event, task, and project page every hour — O(pages) reads.

**Mitigation options:**
- ~~Local caching with manifest-hash invalidation: `LapisClient.manifest()` returns a single hash; only re-read when it changes. This collapses poll-when-idle to one cheap call.~~ **✅ IMPLEMENTED** — `HttpLapisClient` now caches file reads (`_file_cache`) and manifest listings (`_manifest_cache`). Cache is invalidated on writes or when `_refresh_manifest()` detects a changed manifest hash. `list_files()` only re-fetches when dirty. `is_reachable()` added for health checks.
- Move the trigger queue to a lightweight sidecar (Redis, SQLite) and keep Lapis for durable knowledge only.

### 4. gowa rate limiting

**What:** `gowa_client.py` enforces a 1.5s gap between sends. Flushing 10 notifications to 3 groups each = 30 sends = 45 seconds minimum.

**Risk:** The flush job runs every 2 minutes. If outbound volume exceeds what can be sent in 2 minutes, the queue grows unboundedly.

**Mitigation options:**
- Batch outbound: group multiple notifications for the same JID into a single message (already partially done by summariser, but only within one flush cycle).
- Priority queue: send high-urgency first, batch/defer normal.
- Backpressure: if the queue depth exceeds a threshold, lengthen the flush interval rather than hammering gowa.

---

## Race Conditions

### 1. Read-modify-write on shared wiki pages

**Where:** `outputmessages.md`, project pages (two updates to the same project in the same batch), the seen-state file.

**Scenario:** Agent A reads `outputmessages.md`, appends entry. Before A writes, Agent B reads the same (old) version, appends its own entry. B writes. A writes. B's entry is lost.

**Current guard:** `threading.Lock` per file path — works within one process only.

**Fix:** Optimistic concurrency on Lapis writes (read hash, conditional PUT, retry on conflict). The `LapisClient` already has `manifest()` — extend `write_file` to accept an expected hash and retry.

### 2. Mark-seen timing

**Where:** `pipeline.py` line 97-99. IDs are marked seen *after* the entire batch succeeds.

**Scenario:** Batch of 20 messages; message #15 triggers a Lapis write that fails (network blip). The batch aborts. All 20 messages (including the 14 that succeeded) are reprocessed on the next run. The 14 successful signals are written again — duplicate project updates, duplicate research entries.

**Current guard:** None (crash-reprocess is the design, but it's not idempotent).

**Fix:** Make wiki writes idempotent: `append_project_update` should check if an update with the same source_message_ids already exists before appending. Or mark seen per-message after each successful write rather than per-batch.

### 3. Scheduler thread vs. webhook thread

**Where:** `scheduler.py` + `webhook.py` Flask app run in the same process. The scheduler thread calls `pipeline.ingest_batch()` (for age-triggered buffers) while the Flask thread also calls `ingest_batch()` (for count-triggered buffers via the webhook).

**Scenario:** Both threads call `ingest_batch` for the same group at the same time. The GroupBuffers `ready_batches()` pops the buffer, so they shouldn't get the same messages — but the pipeline's internal state (group context updates, seen-set writes) is not thread-safe.

**Fix:** Either serialize all `ingest_batch` calls through a queue (producer-consumer), or add a per-group lock in the pipeline.

---

## Edge Cases

### 1. Group JID changes

**What:** WhatsApp occasionally changes a group's JID (when a group is "restarted" by an admin, or migrated). The old JID stops receiving messages; the new JID starts.

**Impact:** The allowlist, group registry, per-group context, and seen-state all key on JID. A JID change silently stops ingestion for that group and starts treating the new JID as unknown (dropped by allowlist).

**Mitigation:** Log unknown-group messages that would have been dropped. Periodically reconcile the registry against gowa's actual group list (`GET /groups`).

### 2. gowa session drop

**What:** gowa's WhatsApp Web session can drop (phone offline too long, WhatsApp Web limit reached, account banned). When it reconnects, it replays buffered messages — potentially hundreds.

**Impact:** A reconnect replay can flood the pipeline. The dedup stage catches re-delivered IDs, but if the seen-state file was lost or corrupted, everything reprocesses.

**Mitigation:** Cap the ingest batch size (e.g., process oldest 50, defer the rest). Add a health-check endpoint that alerts when gowa's session status changes.

### 3. LLM misclassification

**What:** The classifier may return wrong signal types (a casual message classified as a project update, or a real event missed as noise).

**Impact:** False positive: junk written to the wiki and triggers fired for nothing. False negative: a real signal is dropped.

**Mitigation:** The stub classifier's heuristics catch the obvious cases without LLM. For the LLM path: log every classification decision with the input thread, so misclassifications can be audited. Add a confidence threshold — below it, write to a "needs review" queue rather than acting.

### 4. Frontmatter corruption

**What:** `mdfront.py`'s naive YAML parser handles simple cases but doesn't support all YAML features. A message containing `---` on its own line, or frontmatter with complex nested structures, could break parsing.

**Impact:** `_read_output_queue` returns an empty queue (the fallback), silently dropping pending notifications.

**Mitigation:** Install PyYAML (`pip install pyyaml`) in production — `mdfront.py` already prefers it when available. Add a validation step: if parsed queue length drops unexpectedly, log a warning rather than silently proceeding.

### 5. Message ordering across groups

**What:** `GroupBuffers` flushes each group independently. If two groups discuss the same topic and one flushes before the other, the classifier sees partial context.

**Impact:** Duplicate or contradictory signals — e.g., a research paper shared in both the ML group and the TLDR group gets two separate `research_paper` signals.

**Mitigation:** Dedup signals by content hash (URL, paper title) across groups within a time window. The seen-ID mechanism deduplicates messages but not cross-group semantic duplicates.

### 6. Reminder dedup — **✅ IMPLEMENTED**

**What:** The reminder job runs hourly and reads all upcoming events and pending tasks. It generates new NotificationIntents each run, with fresh UUIDs.

**Impact:** The same event reminder is sent every hour until the event passes. Same with stale-project nudges.

**Mitigation:** ~~Track which reminders have been sent (by event/task ID + date) in a state file. Only re-send if the event is within a closer window than last time (e.g., 3 days → 1 day → today).~~ **✅ IMPLEMENTED** — `ReminderState` class in `reminders.py` tracks sent reminders using window brackets `[0, 1, 3]` days. Events re-fire only when they cross into a tighter bracket. Task digest: once per day. Project nudges: once per day per project. State persisted to `WhatsApp/.state/reminder_state.json` via Lapis, auto-pruned after 30 days.

### 7. Empty/missing `meta/groups.md`

**What:** If `meta/groups.md` doesn't exist or is empty, `GroupRegistry.from_wiki()` returns an empty registry.

**Impact:** `allowlist()` returns `{}`, so all messages are dropped. `announce_groups()` returns `[]`, so outbound notifications have no target and are silently skipped.

**Current guard:** `run_live.py` falls back to `WA_ALLOWLIST` env var, but the registry is still empty (no project→group mapping, no announce target).

**Mitigation:** Log a clear warning on startup when the registry is empty. Fall back to treating all allowlisted groups as announce targets.

---

## Recommendations (priority order)

1. **Make wiki writes idempotent** — check for duplicate source_message_ids before appending. Cheapest fix, biggest reliability gain.
2. ~~**Add optimistic concurrency to Lapis writes**~~ — **partially addressed**: per-agent output sub-paths eliminate cross-agent contention on the output queue. Intra-agent optimistic concurrency (conditional PUT with manifest hash) still a good hardening step for project pages.
3. **Parallelize LLM classification** — `ThreadPoolExecutor` for classify calls. Straightforward 5-10x throughput gain during surges.
4. ~~**Reminder dedup state**~~ — **✅ DONE**: `ReminderState` with window brackets, persisted to wiki.
5. **Serialize pipeline access** — per-group lock or a single ingest queue between the scheduler and webhook threads.
6. ~~**Health monitoring**~~ — **✅ DONE**: `/health` endpoint in `webhook.py` checks gowa session, Lapis connectivity, queue depth, inbound buffer depth, and classification stats.
