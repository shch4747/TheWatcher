"""Model-call logging (Spec: "every model call is logged with tokens and
cost"). Its own leaf module so both `interface.py` and `benchmark.py`
can depend on it without a cycle (benchmark reuses `generate` and
`interface` re-exports it).
"""
from __future__ import annotations

from shared.db import ModelCall, get_session
from shared.models.schemas import TextResult
from shared.models.text import TextModelClient


async def log_model_call(
    tier: str, model_name: str, input_tokens: int, output_tokens: int, cost_usd: float | None = None
) -> None:
    async with get_session() as session:
        session.add(
            ModelCall(
                tier=tier,
                model_name=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
            )
        )
        await session.commit()


async def generate(client: TextModelClient, tier: str, prompt: str, system: str | None = None) -> TextResult:
    """Call a Worker/Mentor client and log the usage - the one path every
    caller should use instead of client.generate() directly, so nothing
    forgets to log (Spec: "every model call is logged")."""
    result = await client.generate(prompt, system=system)
    await log_model_call(tier, client.model_name, result.input_tokens, result.output_tokens)
    return result
