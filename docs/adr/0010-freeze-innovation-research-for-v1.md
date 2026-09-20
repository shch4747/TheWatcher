---
status: accepted
date: 2026-09-20
---
# Innovation Agent and Research Agent are frozen for the duration of v1

The Spec places Research Agent and Innovation Agent under "Out of Scope (v1)" / "After v1": v1 proves the WhatsApp → wiki pipeline (Gateway, WA Agent, Scheduler, Wiki layer, Project Agent v1) and nothing else. `agents/innovation-agent/` and `agents/research-agent/` predate the pivot and are real, if pre-pivot, prototypes — not scratch code — so they are kept, not deleted, but frozen: no refactors, no new features, no dependency upgrades, no attempt to bring them into line with ADR-0003/0005/0006/0008 until v1 ships (Plan phases 0-6).

The only permitted change to either folder before then is mechanical relocation (e.g. `git mv` for repo layout changes) that does not alter file contents.

**Consequences**: contributors working on v1 must not "clean up" or extend these two folders as a side effect of unrelated work; any real interest in resuming them is a Phase-7-or-later planning decision, not something to pick up opportunistically mid-v1. Each folder carries a `FROZEN.md` pointing back here so the constraint is visible without reading the ADR index first.
