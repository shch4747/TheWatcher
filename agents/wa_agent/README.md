# WA Agent

Ingestion (batch cutting, thread assignment, thread writing) and the Chat
Agent (mention/reply handling, Proposal writes). See
[`docs/Spec - Watcher v1.md`](../../docs/Spec%20-%20Watcher%20v1.md)
("WhatsApp Agent — ingestion" / "— Chat Agent") and
[`docs/Plan - Watcher v1.md`](../../docs/Plan%20-%20Watcher%20v1.md)
(Phases 2, 4, 5).

The Gateway (webhook intake, `send`, commands) lives in `shared/gateway/`,
not here — this package only ever calls it through
`shared/gateway/interface.py`, never `gowa_client` directly (ADR-0001,
ADR-0003).

Currently: Phase 0 skeleton only (`interface.py` is empty).
