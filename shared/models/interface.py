"""Models interface (ADR-0006: three tiers behind one interface). Every
call is logged with tokens and cost to the run ledger. Decision Model
first for bounded questions with a confidence threshold and Worker
fallback below it (Spec: WhatsApp Agent ingestion); Worker/Mentor for
open-ended text, escalating to Mentor for high-stakes or low-confidence
items.
"""
from __future__ import annotations

from shared.config import settings
from shared.db import ModelCall, get_session
from shared.models.decision import DecisionModelProtocol, FixtureDecisionModel, JevClient
from shared.models.schemas import ChoiceResult, NoulResult, ScoreResult, TextResult
from shared.models.text import TextModelClient, mentor_model, worker_model


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


async def decide_with_fallback(
    decision_client: DecisionModelProtocol,
    worker_client: TextModelClient,
    question: str,
    options: list[str],
    threshold: float | None = None,
) -> ChoiceResult:
    """The batch-cutting pattern from Spec (WhatsApp Agent ingestion):
    one Decision Model Choice call; below the confidence threshold, the
    Worker Model decides instead by picking from the same option list."""
    threshold = threshold if threshold is not None else settings.decision_confidence_threshold
    result = await decision_client.choice(question, options)
    await log_model_call("decision", "jev", 0, 0)

    if result.confidence >= threshold:
        return result

    prompt = f"{question}\n\nPick exactly one of: {', '.join(options)}. Reply with only the option text."
    text_result = await generate(worker_client, "worker", prompt)
    picked = text_result.text.strip()
    if picked not in options:
        # Worker didn't return a clean option - fall back to the
        # Decision Model's own top pick rather than raising mid-batch.
        return result
    return ChoiceResult(option=picked, probabilities={picked: 1.0})


__all__ = [
    "ChoiceResult",
    "NoulResult",
    "ScoreResult",
    "TextResult",
    "DecisionModelProtocol",
    "JevClient",
    "FixtureDecisionModel",
    "TextModelClient",
    "worker_model",
    "mentor_model",
    "log_model_call",
    "generate",
    "decide_with_fallback",
]
