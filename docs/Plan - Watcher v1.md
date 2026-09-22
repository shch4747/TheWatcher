---
type: note
title: Plan — Watcher v1 phases
status: proposed
date: 2026-09-20
tags:
  - watcher
  - plan
---
# Plan — Watcher v1, phase by phase

Implements [[Spec - Watcher v1]]. Each phase ends with something runnable and demoable; later phases never require rewriting earlier ones. Sizes are rough, for a part-time student team; a phase is one or two people.

## Phase 0 — Skeleton that talks to WhatsApp (≈1 week)
**Goal:** the container runs next to gowa, receives a webhook, logs it, and can send one message.
- Repo: `pyproject` (uv), `src/watcher/{gateway,wa_agent,scheduler,wiki,models,cms,project_agent}` each with an empty `interface.py`; ruff/mypy/pytest in CI; `AGENTS.md`-style contributor rules (module boundaries, no cross-package imports outside `interface`).
- `docker-compose.yml`: gowa (v7+, SQLite, webhook → watcher, HMAC secret, `WHATSAPP_WEBHOOK_EVENTS` set, `WHATSAPP_AUTO_DOWNLOAD_MEDIA=false`) + watcher; volumes for both DBs; `.env.example`.
- Gateway: FastAPI webhook endpoint with signature verification; SQLite schema + migrations (`messages_buffer`, `channels`, `outbound_log`); `send()` with 3–10 s spacing and typing indicator; audit page append.
- Fake gowa (test double) serving the recorded OpenAPI shapes; first end-to-end test: replay one webhook → row in buffer.
- **Exit:** login via QR in gowa; a message in a test group appears in the buffer; `send()` posts a reply; CI green.

## Phase 1 — Wiki layer (≈2 weeks) — *can run in parallel with Phase 0*
**Goal:** code can read and write every page type of the live vault without damaging it.
- Schema loader: Pydantic models per type generated/validated against `meta/schema/*.md` (or hand-written models with a test that they match the tables).
- Parser/serialiser: frontmatter (unknown keys preserved), H2 sections with aliases and owners, fences, item grammar with block ids and inline fields, wikilinks, `[src:: ]`.
- Round-trip test suite: every page in the vault + every template + hand-edit scenarios; byte-identity for untouched sections.
- Lapis client: read with revision, write with base revision, conflict detection; local-directory adapter with the same semantics for tests.
- Lint job: validation, PII detector, additive repairs, `meta/lint.md`.
- Derived regeneration: member sections, initiative `Threads`, channel indexes, root indexes, `README.md`.
- **Exit:** lint runs clean on the live vault; regeneration produces a correct `Members.md` and `README.md`; a deliberately hand-mangled page is flagged, not "fixed".

## Phase 2 — Gateway complete (≈2 weeks)
**Goal:** allowlisting, identity and commands work from a phone.
- Edge filtering (allowlist, DMs, admin commands before allowlisting); dedupe; edit/revoke handling on the buffer; quiet-period timer reset on edits.
- Commands: `/setup` (one-shot and dialogue forms via `setup_sessions`), `/unwatch`, `/status`, `/link`; singleton enforcement for coordis/exes/research/all; channel page + initiative page creation from templates; `meta/channels.md` mirror.
- Member Registry + `cms.get_member` client against the protected CMS endpoints (seeded meanwhile from `~/projects/watcher-seed/members.json`); fuzzy match → admin Proposal → link; `whatsapp:` flag on member pages.
- Proposals table + 👍 reaction handling (reusable by Phase 5); expiry.
- `get_message`, `get_context`, `get_messages`, `request_history` (backfill support).
- **Exit:** `/setup project Watcher` in a real group allowlists it, creates/links pages, replies "watching"; an unknown sender is proposed and linked; all command paths covered by fake-gowa tests.

## Phase 3 — Scheduler (≈1 week)
**Goal:** every "later", "every", "after" and "not concurrently" in the system goes through one place.
- Job registry, run ledger, triggers (`run_at`, `run_every`, `run_after`, `run_now`), dependency rules, per-key locks, retries/backoff; `meta/agents/<agent>.md` regeneration.
- Wire existing jobs: lint (daily), derived regeneration (on write + nightly), audit rollover, coordis lint ping (daily, batched).
- **Exit:** two overlapping triggers for the same key run serially; a failing job retries and its ledger shows it; agent status pages update.

## Phase 4 — Ingestion: batches, threads, notices (≈3 weeks)
**Goal:** a watched group produces correct thread pages and inbox notices without human involvement.
- `models` package: Decision Model client (Jev Choice/Noul/Score, batched), Worker and Mentor adapters behind one interface, per-call cost logging; fixtures for tests. (Cost is measured from the provider response rather than a price table, and attribution rides in a ContextVar — ADR-0013.)
- Batch cutter (N/T/quiet from `meta/config.md`) under the channel lock; backfill batches.
- Thread assignment (Decision Model → Worker fallback, multi-label rule); thread creation with ids/titles; chatter dropping.
- Thread page writing (Summary, Items, Timeline with `[src:: ]` on every line); channel index; cursor; Update Notices; Mentor re-check for decisions/low confidence.
- Stale transition, revival, `ended` detection from wiki edits → archive move; Sunday stale nudge with Lapis links.
- Skills: `SKILL.md` loader (repo + `meta/skills/`), first skills: summarise-thread, extract-items, name-thread.
- **Exit:** replaying 5 recorded real conversations through fake gowa yields thread pages matching golden files (reviewed by humans); a live group runs for a week with the team reading the output.

## Phase 5 — Chat Agent + Project Agent v1 (≈2 weeks)
**Goal:** members can ask and record; project pages stay current.
- Mention/reply detection; thread identification; out-of-cycle thread refresh; read-only QA from the wiki in non-initiative channels; quoting replies.
- Proposal flow for writes (reusing Phase 2), ✅ confirmation, text-approval via Decision Model Noul.
- Project Agent v1: inbox consumption, Item upsert into initiative pages with stable ids, read-back of human edits, `## Status` rewrite, Proposal execution, Events handled identically.
- **Exit:** "@bot who owns the hall booking?" answers from the thread; "@bot note the demo moved to Friday" produces a Proposal that a 👍 turns into a Decisions line; project page tasks reflect chat within one batch.

## Phase 6 — Hardening and handover (≈2 weeks)
**Goal:** cheap, observable, and maintainable by people who didn't build it.
- Worker model benchmark (GPT-6 Luna vs GLM 5.3 Flash vs `claude-haiku-4-5`) on the recorded eval set; pick and record in an ADR; cost report per batch in the run ledger. **Delivered by ADR-0013**, which goes further than "in the run ledger": per-channel timing and per-phase (classification vs summarisation) tokens/cost in `obs_ingest_runs`/`obs_ingest_channel_runs`, a report posted to the logs channel after each run that did work, a provisioned Grafana dashboard over the same SQLite file, an audit row per wiki write in `obs_vault_ops`, and OpenTelemetry traces in a self-hosted Phoenix.
- Live eval set (10–20 conversations with expected threads/items), run manually before releases.
- Operator docs: deploy, re-login after a ban, rotate the number, restore from backup; contributor docs per module.
- HTTP adapter over interfaces (stub for MCP later); security pass (webhook secret rotation, CMS token handling, PII lint in CI).
- **Exit:** a new contributor can add a skill and a command from the docs alone; the team runs it for two weeks across all project groups.

## After v1 (not planned in detail)

`agents/innovation-agent/` and `agents/research-agent/` are frozen for the duration of v1 — see [ADR-0010](adr/0010-freeze-innovation-research-for-v1.md). They are not to be refactored, extended or "cleaned up" as a side effect of v1 work. (Project Agent items below — health score, nudges, GitHub — are v1's own Project Agent folder, deferred rather than frozen.)

Project Agent: health score, nudges via Scheduler, critical analysis, GitHub Projects direction. Research Agent (digests, resources). Innovation Agent (ideas, dependency rules). CMS write endpoints. Media understanding. Self-editing skills / Engineer Agent. Container split + MCP exposure if another team needs it.

## Dependencies at a glance
```
Phase 0 ──┐
Phase 1 ──┴─► Phase 2 ─► Phase 3 ─► Phase 4 ─► Phase 5 ─► Phase 6
```
Phases 0 and 1 are independent and should start together. Everything after depends on both.
