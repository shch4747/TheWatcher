"""Models interface (ADR-0006: three tiers behind one interface). Every
call is logged with tokens and cost to the run ledger. Decision Model
first for bounded questions with a confidence threshold and Worker
fallback below it (Spec: WhatsApp Agent ingestion); Worker/Mentor for
open-ended text, escalating to Mentor for high-stakes or low-confidence
items.
"""
from __future__ import annotations

from shared.config import settings
from shared.models.benchmark import BenchmarkCase, BenchmarkReport, ModelReport, run_benchmark
from shared.models.calls import generate, log_model_call
from shared.models.decision import (
    DecisionModelProtocol,
    FixtureDecisionModel,
    JevClient,
    WorkerBackedDecisionModel,
    default_decision_client,
)
from shared.models.schemas import ChoiceResult, NoulResult, ScoreResult, TextResult
from shared.models.skill_loader import Skill, load_skills, load_skills_from_dir, parse_skill_md
from shared.models.text import TextModelClient, client_for_model, mentor_model, worker_model


async def decide_with_fallback(
    decision_client: DecisionModelProtocol,
    worker_client: TextModelClient,
    question: str,
    options: dict[str, str | None],
    threshold: float | None = None,
) -> ChoiceResult:
    """The batch-cutting pattern from Spec (WhatsApp Agent ingestion):
    one Decision Model Choice call; below the confidence threshold, the
    Worker Model decides instead by picking from the same option list.
    `options` maps each option key to a description (may be `None`) -
    both the Decision Model and the Worker fallback see it, so neither
    is ever asked to choose among bare, undescribed option keys."""
    threshold = threshold if threshold is not None else settings.decision_confidence_threshold
    result = await decision_client.choice(question, options)
    await log_model_call("decision", "jev", 0, 0)

    if result.confidence >= threshold:
        return result

    option_lines = "\n".join(f"- {opt}: {desc}" if desc else f"- {opt}" for opt, desc in options.items())
    prompt = (
        f"{question}\n\nOptions:\n{option_lines}\n\n"
        "Reply with only the option name exactly as written above."
    )
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
    "WorkerBackedDecisionModel",
    "default_decision_client",
    "FixtureDecisionModel",
    "TextModelClient",
    "client_for_model",
    "worker_model",
    "mentor_model",
    "log_model_call",
    "generate",
    "decide_with_fallback",
    "Skill",
    "load_skills",
    "load_skills_from_dir",
    "parse_skill_md",
    "BenchmarkCase",
    "BenchmarkReport",
    "ModelReport",
    "run_benchmark",
]
