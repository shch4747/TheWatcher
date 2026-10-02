# Project Agent

v1 scope only: consumes its Inbox, upserts Items into initiative pages,
rewrites `## Status`. Confirmed Proposals are executed by the WhatsApp
Agent, not here. Health score, nudges, critical analysis and GitHub sync
are explicitly out of v1 scope — see
[`docs/Spec - Watcher v1.md`](../../docs/Spec%20-%20Watcher%20v1.md)
("Project Agent v1" / "Out of Scope (v1)") and
[`docs/Plan - Watcher v1.md`](../../docs/Plan%20-%20Watcher%20v1.md)
(Phase 5).

## Layout

- `interface.py` — public surface: `run_project_agent_once`,
  `apply_thread_items_to_initiative`, `rewrite_status`.
- `agent.py` — `ProjectAgent` (inbox consumption) and one handler per
  item kind (task / decision / resource / question). `_merge_item` stays
  a pure policy function (`merge_item`).
