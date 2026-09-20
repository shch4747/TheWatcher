---
status: accepted
date: 2026-09-19
---
# Raw messages live in gowa; the wiki holds derived knowledge and pointers only

gowa already persists every message it sees. We do not mirror raw messages into the wiki or (for v1) into our own database. The wiki gets Thread Summaries, Channel Pages and Inbox Items, each carrying gowa message IDs as pointers back to the source. Reasons: raw chat logs are large, noisy, and contain PII; the wiki is meant to be *what ARIES knows*, not a transcript; and gowa's store is queryable by JID, time range and text.

**Consequences**: anything that needs original text (re-summarising after an edit, the Chat Agent quoting a message) fetches from gowa via the Gateway. If gowa's query API proves limiting (100 per page, no cross-chat queries) we may add our own mirror — that is a reversible extension, not a change to this rule.
