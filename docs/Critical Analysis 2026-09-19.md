---
type: note
title: Watcher — Critical Analysis (2026-09-19)
tags:
  - watcher
  - design
---
# Critical analysis of the current Watcher design

Scope: the WhatsApp side of [[Watcher]] — the WhatsApp Agent, the "Notification + Scheduling Manager", their relationship with gowa, the interface offered to other agents, and the per-chat / per-thread representation. [[agent-output-guide-1]] is treated as a rough sketch, not a decision.

## Facts established (gowa v7+, Lapis)

- gowa is a single REST process. It pushes HMAC-signed webhooks for `message`, `message.reaction`, `message.edited`, `message.revoked`, `message.deleted`, `message.ack`, `group.joined`, `group.participants`, and more. Events can be filtered by type and by JID.
- gowa keeps its own chat store (SQLite by default, Postgres optional). `GET /chat/{jid}/messages` supports time range, text search, `is_from_me`, pagination (max 100). `POST /chat/{jid}/history` asks the phone for older messages asynchronously (anchored on the oldest stored message).
- Sending supports `reply_message_id` (quote), ghost mentions / `@everyone`, polls, reactions, edit, revoke, media, presence ("typing").
- Message webhook payloads carry `id`, `chat_id`, `from`, `from_lid`, `sender_display_name`, `from_name`, `is_from_me`, `timestamp`, `body`, and for quoted replies `replied_to_id` + `quoted_body`. Media is auto-downloaded to gowa's disk.
- gowa exposes its own MCP server at `/mcp` with 5 consolidated tools (`whatsapp_send`, `whatsapp_message`, `whatsapp_chat`, `whatsapp_group`, `whatsapp_app`).
- gowa is built on whatsmeow (reverse-engineered WhatsApp Web). Meta can and does ban numbers using unofficial clients. This is an accepted-risk item, not a solvable one.
- Lapis: one Durable Object per vault serialises all writes; text files are per-file revisions with 3-way merge; concurrent overlapping edits produce a Conflict Note rather than a silent overwrite. Exposed via HTTP and MCP.

## Problems with the current idea

### 1. "Notification + Scheduling Manager" bundles two unrelated responsibilities
Outbound WhatsApp delivery and "run the Project Agent in 4 hours" have nothing in common. A scheduler is a cross-cutting service used by every agent; putting it inside the WhatsApp box means every agent depends on the WhatsApp module even when it never sends a message. Split them.

### 2. Using wiki files as a message queue is the wrong tool
The output guide turns `WhatsApp/output/<agent>.md` frontmatter into a queue. Consequences:
- **Two writers per file.** The producing agent appends entries; the WA agent flips `sent: true` and prunes. That is exactly the cross-writer read-modify-write race the design says it avoids. On Lapis, an overlapping edit yields a Conflict Note — now your queue has a conflict note.
- **Latency by polling.** ~2 min polling on a store that supports websockets/webhooks.
- **Operational state pollutes the knowledge base.** The wiki is supposed to be *what ARIES knows*, not *what the bots are doing right now*. Queues, `sent` flags, and audit logs are ops data.
- **No schema enforcement.** YAML in frontmatter is a stringly-typed contract for a multi-person project that wants "strict encapsulation".
The same applies to the "input pages" (`Projects/<name>.md` appended by the WA agent, consumed by the Project Agent): the canonical project page is also an inbox, so two modules own the same file.

### 3. Re-phrasing other agents' output through an LLM is a lossy double hop
"The WA agent may rephrase it for the target group's context" means every notification passes through a second model that can distort, soften, or hallucinate. It also costs tokens on every send. Framing ("[from Research]", group-specific prefix) is deterministic templating; the *content* should be final when it leaves the producing agent.

### 4. Raw message storage is undefined
gowa already stores every message. The diagram also routes messages into Lapis. If raw chat logs land in the wiki they are (a) huge, (b) noisy, (c) a privacy problem (personal numbers, private DMs), and (d) duplicated. Decide: gowa store is the raw source of truth; the wiki holds derived knowledge and *pointers* (message IDs, chat JIDs).

### 5. "Thread" is undefined and WhatsApp has no threads
Structure available: quote-replies (`replied_to_id`) give partial explicit linkage; everything else is inferred (topic segmentation is an LLM judgment with no ground truth). Open questions with real design consequences:
- Can a message belong to more than one thread? Can a thread span chats?
- Who decides a thread has ended, and what is the signal (time since last message, explicit resolution, an LLM "this looks done")?
- Is a thread the unit of handoff to the Project Agent, or is a message batch?
- What does "archive" mean concretely — a status field, a move to another page, deletion of the summary?

### 6. Identity is missing
Payloads give a phone JID and increasingly a LID (`@lid`) — with LIDs the phone number may not be present at all. Nothing in the plan maps a sender to a wiki member page (`members/`). Without a registry, every summary says "someone said" and nudges cannot be targeted. Also: phone numbers are PII and should not be written into wiki pages.

### 7. Triggering other agents is hand-wavy
"Trigger the Project Agent if the need arises" — the deciding entity, the signal and the payload are all unspecified. This is the single most important interface in the WA module and it is currently a sentence.

### 8. Idempotency and mutation are not handled
Webhooks arrive at-least-once and possibly out of order. Edits and revokes mutate history *after* it may have been summarised. Any batch summariser needs: dedupe by message id, a stable cursor, and a way to re-process a window when a message in it is edited/revoked.

### 9. Cold start and group onboarding
Adding the bot to a group with two years of history: do we backfill (gowa can pull older history), summarise only from now, or ask a human? "Automatic project setup when added to a group" assumes every group is a project; many won't be (coordi, exes, announcement, events).

### 10. Chat agent scope and safety
An autonomous responder on a real WhatsApp account in real club groups. Needs: a dedicated number (not a member's), an allowlist of chats, rate limits on outbound, an explicit "must be @mentioned or replied-to" gate, and a defined verb list for actions. Ban risk means the number should be disposable.

### 11. Interfaces are described as files, not contracts
"Strict encapsulation" for a large team needs typed contracts (HTTP/JSON with schemas, or MCP tools) that can be versioned and tested. gowa already exposes MCP; Lapis exposes MCP; the WA module exposing its interface as a small HTTP/MCP surface fits the existing pattern. Files can remain the *audit* medium.

### 12. The weak-LLM + mentor pattern has no home yet
Meeting 3's "cheap model does the heavy lifting, escalates to a strong one" is right for ingestion (classification/segmentation is the token sink) but the design does not say where escalation happens or on what signal.

## What is good and should be kept
- Modules with public interfaces and clear ownership.
- Wiki (Lapis) as the shared long-term memory, readable by humans and agents.
- Batch ingestion rather than per-message LLM calls.
- Asking clarifying questions in chat instead of assuming.
- Per-agent output ownership (the *intent* of the output guide) — only the transport is wrong.
