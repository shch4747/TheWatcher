---
status: accepted
date: 2026-09-19
---
# Modules talk through typed async interfaces; wiki files are bookkeeping only

An earlier sketch ([[agent-output-guide-1]]) used wiki markdown files as message queues (`WhatsApp/output/<agent>.md` with a `queue` list and `sent` flags). We are rejecting that: it puts two writers on one file (producer appends, consumer flips flags), relies on polling, has no schema, and fills the knowledge base with operational state. Instead every module exposes a schema'd async interface (functions with typed inputs/outputs, in the spirit of Effect) that other modules call to trigger it or hand it data. The wiki is written *after the fact* for humans and for long-term memory: Channel Pages, Thread Summaries, Inboxes, the Audit Log.

**Consequences**: Inboxes are owned by the consuming agent and written by the WhatsApp Agent through that agent's interface, never by editing the consumer's file behind its back. The concrete transport (in-process calls vs HTTP/MCP across containers) is a separate decision.
