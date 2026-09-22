"""Writing observability rows (ADR-0013).

Kept separate from `interface.py` so `shared.models.calls` can record a
model call without importing the ingest-run machinery, and so nothing
here depends on anything above `shared.db`.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from shared.db import ModelCall, get_session
from shared.observability import context

logger = logging.getLogger(__name__)

OK = "ok"
ERROR = "error"
CONFLICT = "conflict"


async def record_model_call(
    tier: str,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cost_usd: float | None = None,
    cost_source: str | None = None,
    duration_ms: int | None = None,
    outcome: str = OK,
    error: str | None = None,
    attempt: int = 1,
) -> None:
    """One `model_calls` row, with channel/phase/job taken from the
    ambient `context` (see that module for why it isn't a parameter).

    Never raises: a failure to *record* a call must not fail the call.
    """
    ctx = context.current()
    try:
        async with get_session() as session:
            session.add(
                ModelCall(
                    tier=tier,
                    model_name=model_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    cost_source=cost_source,
                    job_name=ctx.job_name,
                    phase=ctx.phase,
                    channel_jid=ctx.channel_jid,
                    ingest_run_id=ctx.ingest_run_id,
                    duration_ms=duration_ms,
                    outcome=outcome,
                    error=error,
                    attempt=attempt,
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - observability must never break the pipeline
        logger.exception("failed to record model call (%s/%s)", tier, model_name)


async def record_vault_op(
    path: str,
    operation: str,
    outcome: str,
    *,
    base_revision: str | None = None,
    new_revision: str | None = None,
    content_bytes: int | None = None,
    content_sha256: str | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    at: datetime | None = None,
) -> None:
    """One `obs_vault_ops` row. Same never-raises contract."""
    ctx = context.current()
    try:
        from shared.observability.models import VaultOp

        async with get_session() as session:
            session.add(
                VaultOp(
                    at=at or datetime.now(UTC),
                    path=path,
                    operation=operation,
                    outcome=outcome,
                    base_revision=base_revision,
                    new_revision=new_revision,
                    content_bytes=content_bytes,
                    content_sha256=content_sha256,
                    duration_ms=duration_ms,
                    channel_jid=ctx.channel_jid,
                    phase=ctx.phase,
                    job_name=ctx.job_name,
                    error=error,
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("failed to record vault op (%s %s)", operation, path)
