# ADR-0015: On-demand ingestion shares the scheduled pipeline and lock

**Status**: accepted  
**Date**: 2026-10-05

## Context

Scheduled ingestion can leave recent messages buffered until the next batch
is ready. A Bot Admin may need the wiki refreshed before asking the Chat Agent
about those messages. The existing `/ingest` command also ran a forced batch
for every watched channel, even when only one channel needed attention.

Request-driven ingestion must not process the same pending messages at the
same time as the scheduled tick or setup backfill. It must also preserve the
existing pipeline's cursor advancement, message consumption, thread updates,
inbox notices, and observability.

## Decision

- `/ingest` is a regex-parsed, Bot Admin-only command that drains pending
  messages for the channel where it was sent. Command parsing and
  authorization do not call a model.
- The Chat Agent exposes an `ingest_channel` tool only to Bot Admins. It may
  target a watched channel by JID, exact channel title, or initiative name;
  ambiguous names return candidates instead of choosing one.
- Both request paths call the existing `IngestionPipeline.run_drained` for
  the selected channel. The existing batch size and drain safety bound apply.
- The composition root executes the operation under the Scheduler's existing
  `ingest_tick` lock key. The new non-blocking lock path reports busy rather
  than waiting behind another ingestion run.
- The Chat Agent's thread listing orders open threads by most recent message.
  Other Thread Store callers keep the existing slug order by default.

## Consequences

On-demand ingestion can refresh one channel immediately while scheduled
ingestion remains the fallback for remaining pending messages. A request is
rejected as busy while any job holds the shared global ingestion lock. As
with other pipeline work, a process crash after a wiki write but before the
buffer is marked consumed can leave a retry window.
