---
status: accepted
date: 2026-09-19
---
# Waiting on humans is modelled as durable rows woken by events, never as suspended agent runs

Many workflows wait on a person over WhatsApp: a Proposal awaiting 👍, a clarifying question ("who owns this task?"), a nudge to re-check in 4 hours, a `/setup` dialogue, a health-score analysis awaiting the lead's reply. The obvious framework answer is a checkpointed graph with `interrupt()`/resume. We rejected that.

Every such wait here has the same properties: it lasts hours to days, the answer may never come or may come from someone else, the in-flight state is tiny (an item id, a question, a step enum), and the authoritative state already lives in the wiki, the kanban or our DB. A suspended run resumes with the worldview it had when it paused; a fresh run reads the current world — and in a club where plans change between Tuesday and Thursday, the fresh run is more correct. Checkpoints also freeze the code version: resuming after a schema or node change breaks, and with nudges and questions always outstanding we would never have a clean deploy.

So: a pending wait is a row (`proposals`, `pending_questions`, `setup_sessions`, scheduled checks) with a small state enum. It is woken by a Gateway event (a quoted reply, a reaction, a command) or a Scheduler timer, and the handler starts a *new* run that reads the row plus the current world.

**Consequences**: no `interrupt()`-style pause anywhere; every "ask and wait" is designed as ask → persist row → return, plus a handler for the answer and an expiry. Expensive non-waiting runs (e.g. Research over many sources) get durability from idempotency keys, or from DBOS as a step-checkpointing escape hatch — not from this pattern.
