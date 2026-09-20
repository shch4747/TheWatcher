---
name: extract-items
description: Extract tasks, decisions, resources and questions from a batch of messages as thread Items.
---

You are extracting `## Items` for a Watcher thread page. For each
message given, decide whether it contains a task, decision, resource,
or question worth recording; chatter produces no item.

Emit one line per item in this exact grammar:

```
- [ ] text [kind:: task] [owner:: [[Member]]] [due:: YYYY-MM-DD] [src:: id] ^i-xxxx
- [x] text [kind:: decision] [src:: id1, id2] ^i-xxxx
- text [kind:: resource] [src:: id] ^i-xxxx
- text [kind:: question] [by:: [[Member]]] [src:: id] ^i-xxxx
```

Rules:

- Every line carries at least one `[src:: id]` pointing back to the
  message(s) it came from.
- `owner`/`by` are wikilinks to a member's wiki title, never a phone
  number or display name guess - if identity is unclear, omit the field
  rather than guess.
- `^i-xxxx` is a short stable id; reuse an existing item's id if you are
  updating it (same task, reworded), mint a new one otherwise.
- Dates are ISO 8601. Never write PII (phone numbers, emails) into any
  field.
- Tasks start unchecked (`[ ]`); a decision is recorded as done (`[x]`).
