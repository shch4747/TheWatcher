# WA Agent

Ingestion (batch cutting, thread assignment, thread writing) and the Chat
Agent (mention/reply handling, Proposal writes). See
[`docs/Spec - Watcher v1.md`](../../docs/Spec%20-%20Watcher%20v1.md)
("WhatsApp Agent — ingestion" / "— Chat Agent") and
[`docs/Plan - Watcher v1.md`](../../docs/Plan%20-%20Watcher%20v1.md)
(Phases 2, 4, 5).

The Gateway (webhook intake, `send`, commands) lives in `shared/gateway/`,
not here — this package only ever calls it through
`shared/gateway/interface.py`, never `gowa_client` directly (ADR-0001,
ADR-0003).

## Layout

- `interface.py` — the package's only public surface: `run_batch` and the
  thread lifecycle/nudge jobs, plus the Chat Agent entry points.
- `assign.py` — the two structured model calls ingestion makes
  ([ADR-0012](../../docs/adr/0012-structured-assignment-replaces-choice.md)):
  one *assignment* call per chunk of messages (which thread does each
  message belong to, and what new threads does this batch start), and
  one *thread update* call per touched thread (title, Summary, typed
  Items). Both prompts are constants in this module, not skills — the
  `skills/` bundles are for the Chat Agent and the benchmark harness.
- `messages.py` — `BufferedMessage` / `ThreadInfo`, shared by the two
  above.

Ingestion does not use the Decision Model (Jev); only the Chat Agent
does.
