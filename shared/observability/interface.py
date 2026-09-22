"""Observability interface (ADR-0003, ADR-0013): the package's only
public surface. Nothing outside `shared/observability/` imports a
submodule directly.

What it provides:

- **attribution** (`scope`, `current`) - ambient channel/phase/job for
  whatever model call or vault write happens inside the block.
- **recording** (`record_model_call`, `record_vault_op`) - the writes
  behind `shared.models.calls` and `ObservedVaultClient`.
- **cost** (`resolve_cost`) - measured from the provider response, or
  priced from tokens; never a hardcoded table.
- **ingest runs** (`ingest_run`) - per-channel timing and per-phase
  token/cost rollups, plus the logs-channel report the composition root
  sends.
- **tracing** (`setup_tracing`) - pydantic-ai OTel spans to an OTLP
  collector.

This package is a leaf: it depends on `shared.db`, `shared.config` and
`shared.wiki` (for the client protocol it wraps), and on nothing above
them. In particular it must never import `shared.gateway` - the
scheduler imports this package and the gateway imports the scheduler,
so that would be a cycle. Reports are rendered here and *sent* by
`main.py`.
"""
from __future__ import annotations

from shared.observability.context import (
    CHAT,
    CLASSIFICATION,
    DECISION,
    PROJECT_AGENT,
    SUMMARISATION,
    CallContext,
    current,
    scope,
)
from shared.observability.cost import COMPUTED, ESTIMATED, REPORTED, UNKNOWN, resolve_cost
from shared.observability.ingest import (
    ChannelTotals,
    IngestRunRecorder,
    PhaseTotals,
    ingest_run,
)
from shared.observability.migrate import ensure_columns
from shared.observability.otel import setup_tracing
from shared.observability.record import CONFLICT, ERROR, OK, record_model_call, record_vault_op
from shared.observability.report import fmt_cost, fmt_duration, fmt_tokens, render_ingest_report
from shared.observability.vault import ObservedVaultClient

__all__ = [
    "CallContext",
    "current",
    "scope",
    "CLASSIFICATION",
    "SUMMARISATION",
    "DECISION",
    "CHAT",
    "PROJECT_AGENT",
    "resolve_cost",
    "REPORTED",
    "COMPUTED",
    "ESTIMATED",
    "UNKNOWN",
    "record_model_call",
    "record_vault_op",
    "OK",
    "ERROR",
    "CONFLICT",
    "ingest_run",
    "IngestRunRecorder",
    "ChannelTotals",
    "PhaseTotals",
    "render_ingest_report",
    "fmt_cost",
    "fmt_tokens",
    "fmt_duration",
    "ObservedVaultClient",
    "ensure_columns",
    "setup_tracing",
]
