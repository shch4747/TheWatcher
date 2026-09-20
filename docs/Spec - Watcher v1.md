---
type: note
title: Spec — Watcher v1
status: ready-for-agent
date: 2026-09-20
tags:
  - watcher
  - spec
---
# Spec — Watcher v1

Synthesised from the 2026-09-19/20 design session. Vocabulary is [[CONTEXT]]; decisions it rests on are ADR-0001 … ADR-0009 in `docs/adr/`; page formats are [[Wiki Format]] and `meta/schema/`. Companion: [[Plan - Watcher v1]].

## Problem Statement

ARIES has grown into a small organisation whose real coordination happens in WhatsApp groups. Decisions, task assignments, resources and plans are made in chat and then lost: nobody can answer "what is the state of project X", "who owns this", or "what did we decide last month" without scrolling through hundreds of messages. New members and coordinators have no way to catch up. The club wiki exists but nobody maintains it, because maintaining it by hand is a second job.

## Solution

Watcher is a bot on a dedicated WhatsApp number that sits in the club's groups and keeps the ARIES wiki current. It reads each allowlisted group in batches, organises what it sees into per-group Threads (topic-coherent conversations with a lifecycle), writes those Threads into the wiki as typed markdown pages with pointers back to the original messages, extracts tasks, decisions, resources and questions into the relevant project or event pages, and tells other agents what changed. Members can address the bot in a group to ask questions answered from the wiki or to have it record something — it always shows what it intends to write and waits for a 👍 from the lead or an admin. Humans keep editing the wiki freely: every page has a declared format in which agent-owned regions are fenced and everything else is theirs. v1 proves the pipeline **WhatsApp → correct, current wiki**; everything downstream (project health, nudges, research, innovation) builds on trustworthy threads.

## User Stories

### Members in a group
1. As a member, I want the bot to stay silent unless addressed, so that it never adds noise to a group.
2. As a member, I want to @mention the bot with a question and get an answer drawn from the wiki, so that I don't have to search old messages.
3. As a member, I want to reply to a bot message to continue the exchange, so that follow-ups work naturally.
4. As a member, I want to tell the bot "note that the demo moved to Friday" and see it propose the exact wiki change, so that I can trust what it records.
5. As a member, I want a Proposal to go through only when the lead or an admin reacts 👍, so that nobody can rewrite a project page via the bot unilaterally.
6. As a member, I want the bot's replies to quote the message they answer and arrive with a natural typing delay, so that it reads like a participant rather than a firehose.
7. As a member, I want my display name to map to my wiki page automatically, so that summaries name me correctly.
8. As a member, I want my phone number and personal details to never appear in the wiki, so that the wiki can be shared widely.

### Initiative leads
9. As a project lead, I want every batch of chat in my project's group summarised into Threads on the wiki, so that I can see what's being discussed without reading it all.
10. As a project lead, I want tasks mentioned in chat ("Aira, can you book the hall by Wed?") to appear as Items with an owner and due date on the project page, so that nothing is forgotten.
11. As a project lead, I want decisions taken in chat recorded with a pointer to the messages, so that I can prove what was agreed.
12. As a project lead, I want resources (links, papers, repos) shared in chat to be collected on the project page, so that they're findable later.
13. As a project lead, I want questions asked in chat that went unanswered to be visible, so that I can answer them.
14. As a project lead, I want to edit my project's Brief and Notes by hand without the bot ever overwriting them, so that the page stays mine.
15. As a project lead, I want to tick a task box or change a due date in the wiki and have the bot respect it, so that the wiki is the truth for tasks.
16. As a project lead, I want to mark a Thread `ended` from the wiki, so that concluded topics leave the active view.
17. As an event lead, I want the same for my event's group, with a Logistics checklist instead of Open tasks, so that events get the same support as projects.

### Coordinators / Bot Admins
18. As a Bot Admin, I want to send `/setup project Watcher` in a group to start watching it, so that onboarding a group takes one message.
19. As a Bot Admin, I want `/setup` to walk me through kind, lead, brief and timeline when the initiative page doesn't exist yet, so that the page is created correctly.
20. As a Bot Admin, I want the bot to process the last 100 messages on setup, so that a newly watched group isn't empty.
21. As a Bot Admin, I want `/unwatch` and `/status` in a group, so that I can stop or inspect watching without touching a server.
22. As a Bot Admin, I want `/link` to attach an unknown sender to a member page when the fuzzy match is wrong, so that identity is correctable in chat.
23. As a Bot Admin, I want the bot to confirm fuzzy identity matches with me before using them, so that summaries never misattribute.
24. As a Bot Admin, I want a Sunday message in the coordis group listing stale threads with Lapis links, so that I can end them from my phone.
25. As a Bot Admin, I want a daily batched notice in the coordis group when pages are broken or in conflict, so that lint problems get fixed.
26. As a Bot Admin, I want to tune thresholds (stale days, batch size, quiet period, proposal expiry) from `meta/config.md`, so that no deploy is needed to adjust behaviour.
27. As a Bot Admin, I want exactly one channel each of kind coordis, exes, research and all, enforced, so that routing to "the announcements group" is unambiguous.
28. As a Bot Admin, I want DMs to the bot ignored and non-allowlisted groups ignored at the gateway, so that the bot's scope is exactly what we set.

### Wiki readers
29. As any member, I want `README.md` to show this week's active initiatives, tasks due, new threads and upcoming events, so that one page tells me what's happening.
30. As any member, I want each channel page to be an index of its active, stale and archived threads, so that I can browse a group's history by topic.
31. As any member, I want each thread page to have a short summary, its items, and a dated timeline where every line links to the original message id, so that I can go back to the conversation.
32. As any member, I want my member page's Projects, Past projects and Open tasks to be generated for me, so that they're never stale.
33. As any member, I want to create a project, event or member page by copying a template, so that hand-made pages match what the agents expect.
34. As any member, I want a `## Notes` section on every page that both humans and agents can add to, so that nothing gets lost for lack of a place.

### Other agents (Project Agent v1, future Research/Innovation)
35. As the Project Agent, I want to pull my Inbox and receive Update Notices (thread, channel, since-message-id), so that I only read what changed.
36. As the Project Agent, I want to read thread pages and find Items in a fixed line grammar, so that I never need an LLM to parse the wiki.
37. As the Project Agent, I want to fetch original messages and their context by id through the Gateway, so that I can resolve ambiguity against the source.
38. As the Project Agent, I want to send a message to a channel through one typed function and get a message id back, so that I never touch gowa.
39. As the Project Agent, I want to execute a confirmed Proposal through the wiki layer, so that ownership rules are enforced for me.
40. As any agent, I want to look up a member by wikilink, CMS id or WhatsApp identity through one function, so that identity is resolved consistently.
41. As any agent, I want to schedule a check ("re-run me for thread T in 4 hours") and rely on locks and retries, so that I never hand-roll timers.
42. As any agent, I want to load skills from the code repo or from `meta/skills/` interchangeably, so that behaviour can be tuned without a deploy.

### Operators (the Watcher team)
43. As an operator, I want the whole system to run as one Docker container next to gowa on a Mac mini or VPS, so that deployment is trivial.
44. As an operator, I want every outbound message in an audit page and every run in a ledger, so that I can trace anything the bot did.
45. As an operator, I want bounded decisions to go to a Decision Model and only open-ended work to a language model, so that ingestion stays cheap.
46. As an operator, I want per-page round-trip tests to run in CI against the real schemas, so that agents can't slowly mangle the wiki.
47. As an operator, I want a fake gowa and a local vault to run the full pipeline offline, so that tests and demos don't need a phone.
48. As an operator, I want the number to be swappable and the ban risk contained, so that losing the number costs a re-login, not data.

## Implementation Decisions

### Shape
- One Python application (Pydantic AI 2.x), one Docker container, deployed with gowa via compose. Packages: `gateway`, `wa_agent` (ingestion + chat agent), `scheduler`, `wiki` (parser, serialiser, Lapis client, lint, derived-regeneration), `models` (Decision/Worker/Mentor adapters), `cms` (member lookup), `project_agent` (v1 consumer only). Each package exposes one `interface` module of `async` functions with Pydantic models in and out; nothing else in a package is imported across package boundaries (ADR-0003, ADR-0005).
- A thin HTTP adapter exposes the same interfaces as JSON endpoints (and later MCP tools) for out-of-process callers. v1 has no out-of-process callers; the adapter exists so the contract is exercised.
- State: one SQLite database on a volume. Tables (conceptually): `messages_buffer`, `channels` (allowlist, kind, cursor, initiative), `threads` (id, channel, state, dirty flags, summary_cursor), `members_registry` (wa identity ↔ CMS id ↔ wiki title), `bot_admins`, `proposals`, `pending_questions`, `setup_sessions`, `jobs`, `runs`, `locks`, `outbound_log`, `notices` (inbox). Wiki pages are derived from these plus model output — never the reverse, except `state: ended` on threads and human edits inside grammar (tasks ticked, dates changed), which are read back.

### Gateway
- Receives gowa webhooks (HMAC-verified) for `message`, `message.reaction`, `message.edited`, `message.revoked`, `group.joined`, `group.participants`; filters non-allowlisted chats and all DMs at the edge, except bot commands from Bot Admins (so `/setup` works before allowlisting). Every event is deduplicated by message id and appended to the buffer. Edits and revokes of unprocessed messages update the buffer; of processed messages are ignored, but edits reset the channel's quiet-period timer.
- Interface (illustrative): `send(channel, text, reply_to=None, mentions=[]) -> message_id`; `react(message_id, emoji)`; `set_typing(channel, on)`; `get_message(id)`; `get_context(id, before, after)`; `get_messages(channel, since_id|range, limit)`; `list_channels()`; `set_channel(...)`; `resolve_sender(wa_identity) -> MemberRef | Unknown`; `link_sender(wa_identity, member)`; `is_bot_admin(member)`; `request_history(channel, count)`.
- Outbound: never merges messages; sends immediately; enforces a random 3–10 s gap between any two sends globally and shows the typing indicator for that gap; writes every send to `outbound_log` and to `meta/audit/<yyyy-mm>.md`.
- Commands are parsed by regex (never a model), accepted only from Bot Admins: `/setup [project|event|coordis|exes|research|all|other] [<Title>]`, `/unwatch`, `/status`, `/link <@sender> <[[Member]]>`. `/setup` for project/event with a missing page opens a `setup_session` that asks for lead, brief, timeline in chat; each answer is a normal message routed by `replied_to_id`. Kinds coordis/exes/research/all are singletons; a second `/setup` of one is refused.
- On successful `/setup`: allowlist the channel, create/link the initiative page, create the channel page, enqueue a `backfill` batch of the last 100 stored messages (asking gowa for history first if fewer are stored), post "watching".
- Identity: unknown sender → fuzzy match of display name against member titles (from CMS + wiki) → Proposal to Bot Admins ("Link *Aira J* to [[Aira]]? 👍") → on confirm, registry row; no match → offer to create a member page. Wiki member pages carry `whatsapp: linked|unlinked` only.
- CMS: `cms.get_member(ref)` calls the protected CMS endpoints; results cached with a short TTL. The registry keys on CMS member id. No PII is ever written to the wiki; lint fails a page containing a phone- or email-shaped string.

### Scheduler
- Job registry (name, handler, schedule or trigger), run ledger (started, finished, outcome, error), triggers `run_at`, `run_every`, `run_after(event)`, `run_now`; dependency rules ("before X, ensure Y ran within N days"); per-key locks (e.g. `wa_agent:batch:<channel>`, `project_agent:<initiative>`); retries with backoff; a `meta/agents/<agent>.md` page regenerated from the ledger. No workflow DSL. All human waits are rows + handlers, never suspended runs (ADR-0007).

### WhatsApp Agent — ingestion
- Batch cut per channel: N=40 messages or T=180 min since the first unprocessed message, whichever first, then wait for a 5-min quiet period (no new message or edit); all from `meta/config.md`. Batches are idempotent on message ids and hold the channel lock.
- Per batch: (1) fetch active thread summaries for the channel; (2) one Decision Model request with one *Choice* question per message over {each active thread, `new-thread`, `chatter`} plus a *Noul* "is this addressed to the bot" only for reply interpretation (mention/reply detection itself is deterministic); (3) messages below the 0.6 confidence threshold are decided by the Worker Model; (4) for each affected thread the Worker Model rewrites Summary and Items and appends Timeline lines, every line carrying `[src:: id]`; (5) new threads get id `<yyyymmdd>-<slug>` and a title; (6) Items with kind `decision` or with low Decision Model confidence are re-checked by the Mentor Model; (7) pages written through the wiki layer; channel index regenerated; cursor advanced; Update Notices posted to the Inbox of the initiative's agent (Project Agent for project/event channels; none for other kinds in v1); (8) chatter is dropped.
- A message may be assigned to several threads (multi-label allowed when the top-2 probabilities are both above threshold).
- Thread states: `active` → `stale` after 3 days without messages (configurable), revivable; `ended` only by a human editing `state:`; on the next run an ended thread's file is moved to `channels/archive/<Channel>/` and links regenerated. Every Sunday a job posts to the coordis channel: counts of stale threads and those silent for 7+ days, with up to 10 Lapis links (`https://lapis.dvenom.in/vault/<vault-id>/file/<path>`).
- Backfill batches are flagged so summaries say "(from history)".

### WhatsApp Agent — Chat Agent
- Trigger: the message @mentions the bot or replies to a bot message, in an allowlisted channel, from any member. Deterministic.
- Flow: identify the relevant thread (the quoted message's thread, else the Decision Model picks among active threads); run an out-of-cycle batch for that thread so it is current; then in project/event channels hand the request to the Project Agent, elsewhere answer read-only from the wiki. Replies quote the triggering message.
- Any write is a Proposal: the bot posts the exact change (page, section, lines) and stores a `proposals` row; a 👍 reaction from the initiative lead or a Bot Admin within 24 h executes it; a text reply is interpreted by a Decision Model *Noul* ("is this an approval?"). Expired proposals are dropped with a one-line notice. Confirmations of executed writes are a ✅ reaction, not a message.
- v1 verbs: answer from wiki; create/modify Items (tasks, decisions, resources, questions) on initiative pages; append to `## Notes`. No real-world actions.

### Project Agent v1
- Consumes its Inbox; reads changed thread pages; upserts Items into the initiative page's managed sections (Open tasks, Decisions, Log, Resources) with stable ids; reads back human edits (ticked boxes, changed dates); rewrites `## Status`; executes confirmed Proposals through the wiki layer. Handles both Projects and Events. No health score, nudges or GitHub in v1.

### Wiki layer
- One parser/serialiser for all page types generated from `meta/schema/` definitions: frontmatter (typed, unknown keys preserved), sections by H2 title (aliases, order-free, unknown preserved), owner classes `human | shared | managed | append | derived`, fences `<!-- watcher:managed|append|derived -->` … `<!-- /watcher -->`, item grammar `- [ ] text [key:: value]… ^id`, wikilinks everywhere including frontmatter, `[src:: id]` pointers. Round-trip parse→dump→parse must be byte-identical for untouched sections.
- All writes are read-modify-write against Lapis with the base revision; a conflict is never resolved by overwriting; lint surfaces `.sync-conflicts/`.
- Derived regeneration job: member Projects / Past projects / Open tasks, initiative Threads, channel indexes, `Members.md` / `Projects.md` / `Events.md` / `Resources.md`, `README.md`, `inbox/*.md`, `meta/channels.md`, `meta/agents/*.md`. Touches only derived sections.
- Lint job: validates every page; reports to `meta/lint.md`; repairs only additive defaults (missing optional keys, missing managed-section skeletons); daily batched ping to coordis for broken pages and conflicts; PII detector.
- Templates in `meta/templates/` are validated by lint like any page.

### Models
- Decision Model: Jev via a small in-house client (Choice/Noul/Score), batched per request, with a confidence threshold of 0.6 and Worker fallback. Worker Model: a cheap model, candidates to benchmark in Phase 6 (user's shortlist: GPT-6 Luna, GLM 5.3 Flash). Mentor Model: `claude-sonnet-5`. All behind one `models` interface so tiers are swappable per config; Pydantic AI agents take the model per constructor.
- Every model call is logged with tokens and cost to the run ledger.

### Skills
- `SKILL.md` folders (frontmatter `name`, `description`; body = instructions), loaded from the repo's `skills/` and from the wiki's `meta/skills/`; wiki wins on name collision. Skills are prompt/instruction bundles for the Worker/Mentor agents (e.g. "summarise a thread", "extract items"), not code.

## Testing Decisions

- A good test drives the system at its edge and asserts on externally visible results: webhook payloads in → wiki files, outbound calls and DB rows out. No test asserts on prompts, internal function calls or intermediate state.
- **Seam 1 (primary): gowa ↔ vault.** A fake gowa (HTTP server serving the recorded OpenAPI shapes: webhooks out, `/send/*`, `/chat/{jid}/messages`, `/message/{id}/reaction`) on one side; a local directory standing in for Lapis (same read/write/base-revision semantics) on the other. Whole-pipeline tests replay recorded conversations and compare the resulting vault to golden files. This is the seam for ingestion, commands, proposals, backfill, stale handling, nudges and the chat agent.
- **Seam 2: page parser round-trip.** Property and golden tests over every page type and every template: parse→dump→parse is byte-identical for untouched sections; hand-edit scenarios (ticked box, reworded task, added `## Notes` line, renamed file) are read back correctly or flagged.
- **Seam 3: module interfaces.** Contract tests per `interface` module using Pydantic models; the HTTP adapter is tested by the same contracts.
- Models are replaced in tests by recorded fixtures (Decision Model responses as probability tables; Worker/Mentor outputs as stored text). A small live-model eval set (10–20 recorded conversations with expected threads/items) runs manually, not in CI.
- Prior art: none in this repo (new codebase). The Lapis repo's worker tests are the reference for style (Vitest there; pytest here).

## Out of Scope (v1)

- Project Agent beyond wiki maintenance: health score, nudges, critical analysis, similarity/merging, GitHub Projects sync.
- Research Agent, Innovation Agent, Engineer/self-updating agent.
- Real-world actions by the Chat Agent (scheduling, poking people, GitHub, sending on behalf of members).
- Media (images, documents, voice) understanding; media messages are recorded in timelines as "[image]" with their id only.
- Cross-channel threads; per-message real-time processing; DMs.
- CMS write endpoints; alumni and past events data; multiple WhatsApp numbers.
- Splitting into multiple containers; MCP exposure beyond the adapter stub.

## Further Notes

- Seams above are my proposal (the skill asks that they be confirmed): the primary seam is deliberately *the whole pipeline* because the individual modules are thin and the risk is in their composition.
- The wiki has already been migrated to the target formats (`scratchpad/migrate-2026-09-19.py`); Phase 1 must treat the live vault as its first fixture.
- Ban risk on the number is accepted and mitigated only by a dedicated SIM, send spacing and the typing indicator; there is no protocol-level mitigation.
- Open items deferred with owners: GitHub direction for tasks (after v1); worker model choice (Phase 6 benchmark); CMS write endpoints (post-v1); Lapis vault id and base URL are config.
