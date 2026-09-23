---
status: accepted
date: 2026-09-23
---
# An agent's Inbox is its page on the wiki, reached only through `shared/inbox`

Amends ADR-0003 (which rejected wiki files as queues) and Wiki Format's `inbox/<agent>.md` ("derived, read-only mirror of Update Notices").

**Context**: Update Notices lived in a `notices` SQLite table. The WhatsApp Agent inserted rows addressed to `"project_agent"`, and the Project Agent read and consumed the same table directly. There was no module in between. ADR-0003 says an Inbox is "owned by the consuming agent and written by the WhatsApp Agent through that agent's interface", but AGENTS.md forbids either agent package importing the other, so that design had nowhere legal to live. Two bugs followed from having no owner. Notices were marked consumed *before* they were processed, so a failed apply was lost. And every notice re-appended all of a Thread's past decisions, resources and questions to the Initiative page. Separately, the Inbox is something humans should be able to see and add to (a Bot Admin asking an agent to look at something), and a database table isn't visible to anyone.

**Decision**:

1. **`inbox/<agent>.md` is the source of truth.** Page type `inbox`: `## Pending` and `## Done` (managed by the Inbox module) and `## Notes` (shared). Each entry is an Item line, `- [ ] Thread update: <title> [kind:: thread_update] [channel:: <jid>] [slug:: <id>] [thread:: [[channels/…]]] [since:: <msg id>] [trigger:: now] [posted:: …] ^n-…`. The `notices` table is gone.
2. **One module owns the page: `shared/inbox`.** It lives in `shared/` because both the poster and the consumer need it. Callers use `Inbox(vault, agent)`: `post()`, `pending()`, `ack()`. No agent reads or writes an inbox page any other way. ADR-0003's objections to files-as-queues were two writers with no arbiter, polling and no schema. They are answered by a single writer module, revision-checked read-modify-write (a conflict is retried from a fresh read and never overwrites) and the Item grammar as the schema. The rest of ADR-0003 stands.
3. **Ack after success.** Items move from Pending to Done only when the consumer calls `ack()` after the work is applied. An item that fails is still pending on the next run. Done keeps the most recent 50.
4. **Triggering and routine items.** `trigger=True` means "do this now". Posting such an item calls a wake-up that `main.py` registers per agent (`register_consumer`), which asks the Scheduler to run that agent's job on its next poll (`request_run`, within about 30 s) without running it inline in the poster. Routine items wait for the agent's own cadence. The flag is written on the page (`[trigger:: now]`) so a human can tell them apart.
5. **Coalescing.** A notice for a Thread that already has a pending notice is merged into it (the earliest `since` wins, and `trigger` is OR-ed), so a busy Thread is one item, not one per batch.
6. **Humans can post.** A line typed into Pending by hand is read as a `request` item and gets an id when the page is next written. The Project Agent leaves request items for a human in v1. It only acts on `thread_update`.
7. **Applying is idempotent.** The Project Agent carries Items onto the Initiative page by block id. Tasks are upserted. Decisions, resources and questions are appended only if that id (or the same text) isn't there yet.

**Consequences**: every batch that touches a project/event Channel does one extra read and write of `inbox/project_agent.md`, which is posted after the batch is committed. A failed post is reported to the logs channel and not raised, because raising would re-run a batch whose Thread pages were already written. Ingestion posts routine items. `trigger` has no production caller yet: it is the hook for the Chat Agent handing work to the Project Agent. Inbox writes appear in the vault audit (ADR-0013) like any other page.

**Alternatives rejected**: keeping the table and rendering the page from it (two stores to keep in step, and a human's addition to the page would be ignored); putting the Inbox in each consuming agent's package (the poster would have to import the consumer); triggering by running the consumer inline (it would run under the ingest job's lock and make ingestion as slow as the slowest consumer).
