# ADR-0016: Lift the Innovation and Research Agent freeze

**Status**: accepted  
**Date**: 2026-10-07  
**Supersedes**: [ADR-0010](0010-freeze-innovation-research-for-v1.md)

## Context

ADR-0010 froze `agents/innovation_agent/` and `agents/research_agent/` while v1 (Gateway, WA Agent, Scheduler, Wiki layer, Project Agent) was built. v1 is live, and work on both agents has already resumed: the Research Agent's digest writer, the Innovation Agent's remedial lane, and the bridged and frontier lanes now read the real vault through `shared.wiki`. The freeze no longer describes how the repo is being worked on.

## Decision

The freeze is lifted for both agents. They are ordinary agent packages again and can take features, refactors and dependency changes.

Both agents follow the same rules as every other agent: the module-boundary rules in `AGENTS.md` (only a package's `interface.py` is importable from outside; only `shared.gateway`, `shared.models` and `shared.wiki` call external services directly), and docs updated with every change.

The Innovation Agent is being moved off its pre-pivot `MemoryInterface`/`MockMemory` onto `VaultClient` one lane at a time. Remedial, bridged and frontier are done; the event lane is next. Until it moves, `MemoryInterface` stays in place for that lane only.

## Consequences

- The `FROZEN.md` files in both agent folders are removed.
- `docs/Architecture.md` no longer describes the two agents as frozen or unconnected to the shared layer.
- ADR-0010 is kept for history and marked superseded.
