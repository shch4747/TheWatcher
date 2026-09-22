---
status: accepted
date: 2026-09-22
---
# Observability: SQLite + Grafana for aggregates, OpenTelemetry + Phoenix for traces

Amends ADR-0006 (what "every model call is logged" has to mean to be useful) and ADR-0009/ADR-0012 (trace content carries message text).

**Context**: after ADR-0012 the ingestion pipeline worked, and the next question was what it cost. That could not be answered. `model_calls` had `cost_usd` and `job_name` columns that no caller ever populated; no row recorded which channel or which phase of ingestion a call belonged to; `_ingest_tick` computed a `BatchResult` per channel and discarded it; nothing timed a channel's batch. The table was worse than empty — it was misleading:

- `decide_with_fallback` wrote a hardcoded `ModelCall("decision", "jev", 0, 0)` on **every** call, including when the client was `WorkerBackedDecisionModel`, which logged its own real worker call. Every fallback decision was double-counted with one zero-token junk row. On the live database those were ~550 of ~1100 rows.
- `JevClient.choice/noul/score` logged nothing at all.
- A model call that *raised* logged nothing, and `assign.py` swallows exactly that exception on retry — so a burned provider request was invisible precisely when things were going wrong.

Separately, nothing recorded what the agents changed in the wiki. Lapis has revisions, but there was no way to ask "how many writes did last night's run make, and how many hit conflicts".

**Decision**: two layers, because they answer different questions.

1. **Aggregates live in the app's own SQLite database, read by Grafana.** New tables `obs_ingest_runs` (one row per tick that did work), `obs_ingest_channel_runs` (one per channel per run — duration, message/thread counts, and tokens+cost split by phase) and `obs_vault_ops` (one per wiki write/delete). Grafana reads the file directly through the `frser-sqlite-datasource` plugin; datasource and dashboard are provisioned from `shared/observability/grafana/`. No exporter, no scrape endpoint, no second datastore. The DB moves to WAL so Grafana's reads and the app's writes don't block each other.

2. **Traces are OpenTelemetry, exported to Arize Phoenix.** Every model call already goes through a pydantic-ai `Agent`, and pydantic-ai emits GenAI semantic-convention spans natively, so this is `Agent.instrument_all()` plus an OTLP exporter. Phoenix is one container, OTLP in, SQLite-backed. Nothing is Phoenix-specific: `OTEL_ENDPOINT` points at any OTLP/HTTP collector.

3. **Attribution rides in a `ContextVar`, not in signatures.** `shared/observability/context.py` holds channel/phase/job/run; scopes are opened at four boundaries that already existed (the scheduler's handler call, the ingest tick, `run_batch`, and the two `generate_structured` call sites in `assign.py`). `asyncio` copies context per task, so concurrent batches never cross-attribute. The alternative — threading a context object through every call — would have touched a dozen signatures across three packages to deliver the same rows.

4. **Cost is measured, never guessed from a hardcoded table.** `RequestUsage.cost` when the provider reports it (OpenRouter always does now — its `usage.include` flag is deprecated and always-on), else `genai_prices` prices the tokens, else `NULL`. Every row records which of those happened in `cost_source`, so a dashboard can tell a measured cost from an estimate. A missing cost is stored as missing, never as zero.

5. **The four accounting bugs are fixed**: the bogus `decide_with_fallback` row is gone, `JevClient` logs its own calls at a configured flat rate (`JEV_COST_PER_CALL`, marked `estimated` — Jev bills per decision and reports no tokens), `WorkerBackedDecisionModel` runs under a `decision` phase scope so Decision-tier work is distinguishable from real Worker work, and failed calls are logged with `outcome='error'`.

6. **The wiki audit wraps the client, not the call sites.** `ObservedVaultClient` implements the four-method `VaultClient` protocol and is applied inside `default_vault_client()`, which every agent, command and job already uses — so the audit is complete without touching a single call site, and works for both `LapisClient` and `LocalDirClient`. Page content is not copied into the audit table (Lapis versions it already); the row keeps a byte count and a SHA-256 so churn is measurable without the database ballooning.

7. **The logs-channel report fires only for runs that did work.** The tick runs every two minutes and almost always finds nothing — reporting unconditionally would post ~720 WhatsApp messages a day. `OBS_REPORT_MIN_MESSAGES` (default 1) is the threshold. The report is *rendered* in `shared/observability/report.py` and *sent* by `main.py`: observability must not import `shared.gateway`, because the scheduler imports observability and the gateway imports the scheduler.

**Consequences**: three new dependencies (`genai-prices` promoted from transitive to explicit, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`) and two new compose services. Grafana's mount of the data volume is read-**write**, because WAL needs to create `-wal`/`-shm` sidecars and the plugin fails on WAL databases in read-only directories; Grafana only issues SELECTs, and that tradeoff is documented in Operations. Traces carry prompts and WhatsApp message text by default (`OTEL_INCLUDE_CONTENT`) — that is the point of tracing and is consistent with ADR-0012's keep-the-PII stance for this internal deployment, but it does mean member content reaches the trace store, so it is a setting rather than a hardcoded `True`. Observability never raises: a failure to record is logged and swallowed, and `setup_tracing()` degrades to no-op if the collector or the packages are missing.

There is no Alembic in this project (`create_all` only, which never ALTERs), so the new `model_calls` columns land via `shared/observability/migrate.py::ensure_columns` — idempotent `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` on every startup. `scripts/backfill_observability.py` marks pre-cutover rows `phase='legacy'` so dashboards can exclude them without deleting history.

**Alternatives rejected**: Prometheus (pull-based counters can't express per-run cost attribution, and it is another datastore to run); Langfuse (Postgres + ClickHouse — wrong weight class for a single-container app); SigNoz (ClickHouse, same); a hardcoded per-model price table (rots the moment a provider changes pricing, and the provider already tells us the real number).
