# Watcher — WhatsApp context

Vocabulary for the WhatsApp side of Watcher: the modules that sit between gowa and the rest of the agents, and the way conversations are represented in the ARIES wiki. Glossary only — no implementation details.

## Modules

**Gateway**:
The I/O module wrapping gowa. Delivers outbound messages, receives inbound events, owns the buffer of messages not yet ingested (hands them out as pending messages, takes them back as consumed), serves message reads (by ID or range) to any agent, and keeps the Member Registry. Contains no model and makes no Proposals.
_Avoid_: notification manager, WA notification + scheduling manager

**WhatsApp Agent**:
The model-driven module that ingests Batches from Channels, maintains Threads and Channel Pages, posts Update Notices to other agents' Inboxes, and owns Proposals.
_Avoid_: WA agent, ingestion pipeline (that is one part of it)

**Chat Agent**:
The part of the WhatsApp Agent that acts when a member addresses the bot in a Channel. A message addresses the bot iff it @mentions the bot or replies to a bot message — deterministic, no model. It first brings the relevant Thread up to date, then hands over to the Project Agent (which owns Projects and Events) in a project/event Channel, or answers read-only from the wiki elsewhere. It never writes to the wiki without a confirmed Proposal. Taking real-world actions (scheduling, poking people, GitHub) is a later goal.

**Scheduler**:
The module every agent uses to run things later, on a cadence, in order, or exclusively: triggers, a run ledger, dependency rules, per-key locks, retries, and "run now". Independent of WhatsApp.

## Models

**Decision Model**:
A bounded-choice model (Jev) that returns a typed answer with probabilities: pick-one, yes/no, or score. Used wherever the answer space is known up front — thread assignment, chatter detection, routing, escalation gating.
_Avoid_: classifier, router (say what is being decided)

**Worker Model**:
The cheap language model that does the bulk of summarising and extraction.

**Mentor Model**:
The strong language model the Worker Model escalates to on low confidence or on high-stakes items.

## Conversation model

**Channel**:
A WhatsApp group the bot has been added to and that is on the Allowlist. Has a Kind and may be linked to an Initiative.
_Avoid_: chat, group (use "group" only for the raw WhatsApp object before it is allowlisted)

**Kind**:
What a Channel is: `coordis` | `exes` | `research` | `all` | `project` | `event` | `other`. There is exactly one Channel each of kind `coordis`, `exes`, `research` and `all`; `project` and `event` Channels are linked to their Initiative.

**Allowlist**:
The set of Channels the WhatsApp Agent ingests from and the Chat Agent responds in. Groups not on it are ignored. DMs are never on it.

**Bot Admin**:
A member allowed to issue bot commands (`/setup`, `/unwatch`, `/status`, `/link`).

**Thread**:
A topic-coherent sequence of messages within one Channel. States: `active`; `stale` (no messages for 3 days, configurable — revivable); `ended` (concluded — decided only by humans, never by a model; a continuation is a new Thread linking to it). Threads never span Channels. A message usually belongs to one Thread but may belong to several.
_Avoid_: topic, conversation

**Chatter**:
Messages that belong to no Thread (greetings, reactions-as-text, "ok"). Stay in gowa, never reach the wiki.

**Thread Summary**:
The wiki representation of a Thread: title, state, what it is about, its Extracted Items, and gowa message IDs pointing to the underlying messages. The messages themselves stay in gowa.

**Extracted Item**:
A structured point pulled out of a Thread: a task, decision, update, resource, question or idea, with the people involved and source message IDs.
_Avoid_: output, payload, summarised point

**Channel Page**:
The wiki page for a Channel: an index of its Threads (active, stale, archived) and its identity. No prose summary.

**Batch**:
The set of new messages from one Channel processed together, cut on N messages (40) or T minutes (180), whichever first, and only after a quiet period (5 min with no new message or edit). All configurable.

**Cursor**:
The point in a Channel's (or Thread's) history up to which a consumer has read.

## Hand-offs

**Inbox**:
An agent's list of things to look at, kept as its page `inbox/<agent>.md` on the wiki - the source of truth, readable by humans, who may add to it. Items stay pending until the agent acknowledges them after doing the work.
_Avoid_: queue, notices table

**Inbox Item**:
One entry in an Inbox. Either an Update Notice or a request a human typed in.

**Triggering item**:
An Inbox Item marked as work to do now: posting it wakes the agent instead of waiting for its next scheduled run. Every other item is routine.

**Update Notice**:
The Inbox Item "Thread X in Channel Y has new material from Cursor Z". A second notice for a Thread that already has one pending is merged into it.

**Proposal**:
A write an agent wants to make to the wiki (or the Member Registry), shown in the Channel and held (24 h) until the Initiative lead or a Bot Admin confirms with a 👍 reaction. Owned by the WhatsApp Agent.

**Audit Log**:
The wiki record of what the Gateway sent and on whose behalf. Bookkeeping, never a queue.

## Initiatives

**Initiative**:
The common shape shared by a Project and an Event: a lead, a brief, a timeline, a status (`proposed | active | paused | done | dead`), members, and optionally a Channel.
_Avoid_: BaseProject (code name, not a domain word)

**Project**:
An Initiative with ongoing work: tasks, a kanban, repositories, a health score.

**Event**:
An Initiative anchored on a date: venue, registrations, logistics.

## Wiki

**Page Schema**:
The declared shape of one page type: its frontmatter keys and its sections with their owners. Lives in `meta/schema/`; the code follows it.

**Section Owner**:
Who may write a section: `human` (agents only Propose), `shared` (both; `## Notes` on every page), `managed` (an agent rewrites it wholesale), `append` (an agent adds lines), `derived` (a job regenerates it).

**Fence**:
The HTML-comment pair that marks the agent-written region of a managed, append or derived section. Outside a fence is human territory.

**Item**:
One line in a managed or append section with a block id and inline fields: a task, decision, resource, question, idea or update.
_Avoid_: entry, bullet

**Thread Store**:
The module that holds one Channel's Threads and its Channel Page as the wiki stores them: where they live, which state each is in, how a new Thread gets its id, how a message is found on one, how Threads go stale, revive and are archived, and how they move when the Channel is retitled. Callers ask it for Threads, never for paths.

**Page editor**:
The only way agent code changes a page. Enforces Section Owners and Fences on every write and refuses content or frontmatter that would break the page's format.

**Lint**:
The job that validates every page against its schema and reports (never repairs, except additive defaults) to `meta/lint.md` and, daily, to the coordis Channel.

## People

**CMS**:
The existing ARIES content-management system on Cloudflare, the store of member details (contact, entry number, hostel, socials). Read by agents through protected endpoints; never copied into the wiki. Later phases add write endpoints.
_Avoid_: member database

**Member Registry**:
The Gateway's mapping from WhatsApp sender identities to CMS member ids (and hence wiki member pages). Lives with the Gateway, not in wiki text; wiki pages never contain phone numbers or LIDs. An unknown sender is fuzzy-matched by display name against member pages and a Bot Admin confirms the match (or a new member page is created).
