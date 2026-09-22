"""What a model call actually cost (ADR-0013).

Deliberately not a hardcoded price table - those rot the moment a
provider changes pricing, and this app talks to whatever OpenRouter
model `WORKER_MODEL_NAME` names.

Two sources, in order of trust:
1. **reported** - the provider told us. Pydantic AI's `RequestUsage`
   carries `cost: Decimal | None`, extracted from the response body;
   OpenRouter returns an authoritative `usage.cost` on every call.
2. **computed** - `genai_prices` (pydantic's own price database, already
   a pydantic-ai dependency) prices the token counts for that model.

Anything else is `(None, "unknown")`: a missing cost is recorded as
missing, never as zero, so a dashboard can't quietly under-report.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

REPORTED = "reported"
COMPUTED = "computed"
ESTIMATED = "estimated"  # a configured flat rate (Jev), not measured
UNKNOWN = "unknown"


def provider_id_for(base_url: str) -> str | None:
    """genai-prices needs to know whose price list to use. The provider
    is whoever `MODELS_BASE_URL` points at."""
    host = base_url.lower()
    for name in ("openrouter", "anthropic", "openai", "groq", "mistral", "deepseek", "together"):
        if name in host:
            return name
    return None


def resolve_cost(usage: Any, model_name: str, base_url: str) -> tuple[float | None, str]:
    """`(cost_usd, source)` for one request's usage. Never raises - a
    pricing lookup failing must not fail the call it is describing."""
    reported = getattr(usage, "cost", None)
    if reported is not None:
        try:
            return float(reported), REPORTED
        except (TypeError, ValueError):  # pragma: no cover - defensive
            pass

    try:
        from genai_prices import calc_price

        calculation = calc_price(usage, model_ref=model_name, provider_id=provider_id_for(base_url))
        return float(calculation.total_price), COMPUTED
    except Exception as exc:  # noqa: BLE001 - unknown model, offline price data, bad usage
        logger.debug("no price for model %r: %s", model_name, exc)
        return None, UNKNOWN
