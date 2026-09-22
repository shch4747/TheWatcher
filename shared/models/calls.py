"""Model-call logging (Spec: "every model call is logged with tokens and
cost"). Its own leaf module so both `interface.py` and `benchmark.py`
can depend on it without a cycle (benchmark reuses `generate` and
`interface` re-exports it).

Every row now carries latency, measured cost, and which
channel/phase/job it belonged to (ADR-0013). The last three come from
the ambient context (`shared.observability.context`) rather than from
parameters, so adding attribution didn't mean changing a dozen call
signatures.

Failures are logged too. They used to vanish: `assign.py` swallows a
failed assignment call and retries, so a burned request left no trace at
all - and the cost dashboard would under-report exactly when things were
going wrong.
"""
from __future__ import annotations

import time

from pydantic import BaseModel

from shared.models.schemas import StructuredResult, TextResult
from shared.models.text import TextModelClient
from shared.observability.interface import ERROR, OK, record_model_call


async def log_model_call(
    tier: str,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float | None = None,
    *,
    cost_source: str | None = None,
    duration_ms: int | None = None,
    outcome: str = OK,
    error: str | None = None,
    attempt: int = 1,
) -> None:
    await record_model_call(
        tier,
        model_name,
        input_tokens,
        output_tokens,
        cost_usd=cost_usd,
        cost_source=cost_source,
        duration_ms=duration_ms,
        outcome=outcome,
        error=error,
        attempt=attempt,
    )


async def generate(client: TextModelClient, tier: str, prompt: str, system: str | None = None) -> TextResult:
    """Call a Worker/Mentor client and log the usage - the one path every
    caller should use instead of client.generate() directly, so nothing
    forgets to log (Spec: "every model call is logged")."""
    started = time.monotonic()
    try:
        result = await client.generate(prompt, system=system)
    except Exception as exc:
        await log_model_call(
            tier, client.model_name, 0, 0,
            duration_ms=_ms(started), outcome=ERROR, error=f"{type(exc).__name__}: {exc}",
        )
        raise
    await log_model_call(
        tier, client.model_name, result.input_tokens, result.output_tokens,
        cost_usd=result.cost, cost_source=result.cost_source, duration_ms=_ms(started),
    )
    return result


async def generate_structured[OutputT: BaseModel](
    client: TextModelClient,
    tier: str,
    prompt: str,
    output_type: type[OutputT],
    system: str | None = None,
) -> StructuredResult[OutputT]:
    """`generate` for a parsed-JSON reply - same logging contract."""
    started = time.monotonic()
    try:
        result = await client.generate_structured(prompt, output_type, system=system)
    except Exception as exc:
        await log_model_call(
            tier, client.model_name, 0, 0,
            duration_ms=_ms(started), outcome=ERROR, error=f"{type(exc).__name__}: {exc}",
        )
        raise
    await log_model_call(
        tier, client.model_name, result.input_tokens, result.output_tokens,
        cost_usd=result.cost, cost_source=result.cost_source, duration_ms=_ms(started),
    )
    return result


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
