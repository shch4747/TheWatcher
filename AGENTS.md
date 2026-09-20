# Contributor rules

This is the v1 application described in `docs/Spec - Watcher v1.md` and
`docs/Plan - Watcher v1.md`. Layout:

- `shared/{gateway,scheduler,wiki,models,cms}/` — cross-cutting packages
  every agent depends on. Each exposes exactly one `interface.py`:
  `async` functions with Pydantic models in and out.
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

## Why

ADR-0001 (gowa as transport), ADR-0002 (gowa is raw source of truth),
ADR-0003 (typed interfaces, not files), ADR-0005 (one Python app,
Pydantic AI) — see `docs/adr/`.
