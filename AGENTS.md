# Contributor rules

This is the v1 application described in `docs/Spec - Watcher v1.md` and
`docs/Plan - Watcher v1.md`. Layout:

- `shared/{gateway,scheduler,wiki,models,cms,observability}/` —
  cross-cutting packages every agent depends on. Each exposes exactly
  one `interface.py`: `async` functions with Pydantic models in and out.
- `agents/wa_agent/` and `agents/project_agent/` — the two agents in v1
  scope, each its own package with its own `interface.py`, owned
  independently.
- `agents/innovation-agent/` and `agents/research-agent/` — **frozen** for
  the duration of v1. See
  [ADR-0010](docs/adr/0010-freeze-innovation-research-for-v1.md). No
  refactors, no new features, no dependency changes — mechanical
  relocation only.

## Module boundaries (ADR-0003, ADR-0005)

- Nothing outside a package imports anything from it except its
  `interface` module. Never `from shared.gateway.gowa_client import ...`
  from `agents/wa_agent` — go through `shared.gateway.interface`.
- No package calls gowa, an LLM provider, or Lapis directly except
  `shared.gateway`, `shared.models`, and `shared.wiki` respectively.
- No cross-imports between `agents/wa_agent` and `agents/project_agent`,
  or between either of them and the frozen agents.
- One Python application, one container (ADR-0005) — not one process per
  agent folder. `agents/*` is an ownership boundary for contributors, not
  a deployment boundary.
- **`main.py` is the one exception**: it's the composition root, and the
  only file allowed to import across both `agents/*` packages and
  `shared/*` in the same place (registering jobs with the Scheduler,
  wiring the Chat Agent as a Gateway message hook). Nothing under
  `shared/` or `agents/` may import `main.py` back — the dependency only
  ever points inward, from the composition root down.

## Observability is a leaf (ADR-0013)

`shared/observability` may import `shared.db`, `shared.config` and
`shared.wiki`, and nothing above them. It must **never** import
`shared.gateway`: the scheduler imports observability and the gateway
imports the scheduler, so that would close a cycle. Anything that needs
to *send* something (the ingest report) is rendered in observability and
sent by `main.py`.

Attribution (which channel/phase/job a model call belongs to) travels in
a `ContextVar`, not in function signatures — open a `scope()` at a
boundary rather than adding a parameter.

## Why

ADR-0001 (gowa as transport), ADR-0002 (gowa is raw source of truth),
ADR-0003 (typed interfaces, not files), ADR-0005 (one Python app,
Pydantic AI), ADR-0012 (structured ingestion, prompts are code
constants, PII is kept), ADR-0013 (observability) — see `docs/adr/`.
