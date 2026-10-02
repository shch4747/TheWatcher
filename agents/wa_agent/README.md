# WA Agent

Ingestion (batch cutting, thread assignment, thread writing) and the Chat
Agent (mention handling, Proposal writes). See
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
  thread lifecycle/nudge jobs, plus the Chat Agent entry points. Classes
  (`IngestionPipeline`, `ChatAgent`, `ProposalService`) live behind these
  functions so `main.py` does not need a flag-day rewrite.
- `ingestion.py` — batch cutting and the ingest transaction. Scheduled vs
  forced cuts are separate methods; non-ingested channels (logs) have
  their own drain path.
- `assign.py` / `assign_models.py` — the two structured model calls
  ingestion makes
  ([ADR-0012](../../docs/adr/0012-structured-assignment-replaces-choice.md)):
  JSON contracts in `assign_models.py`; prompts, chunking, assignment
  loop and item rendering in `assign.py`. Prompts are constants, not
  skills.
- `revise.py` — title/summary guards and the thread-update orchestration.
- `chat.py` — trigger is an @mention (`@watcher` or WhatsApp's
  `@<bot number>`) or a reply to one of the bot's own messages. Replies
  stay off until a Bot Admin sends `/toggle-agent`. Context
  is the last 5 messages plus any message those five reply to.
  Read-only wiki tools (list this channel's threads, search, read a
  page) when that isn't enough. Media-only mentions and replies get a
  canned "can't see that" reply.
- `wiki_tools.py` — the Chat Agent's read-only Lapis tools (no writes).
- `lifecycle.py` — daily stale/archive pass and the Sunday nudge.
- `proposals.py` — proposal lifecycle; persistence is `ProposalStore` on
  the Gateway interface, executors are one function per kind.
- `messages.py` — `BufferedMessage` / `ThreadInfo`.

Ingestion does not use the Decision Model (Jev); only the Chat Agent
does.
