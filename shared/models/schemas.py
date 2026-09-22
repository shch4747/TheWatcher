"""Decision Model question/result shapes (ADR-0006: Choice/Noul/Score,
a known option list, a confidence threshold with Worker fallback)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChoiceResult:
    option: str
    probabilities: dict[str, float]

    @property
    def confidence(self) -> float:
        return self.probabilities.get(self.option, 0.0)


@dataclass(frozen=True)
class NoulResult:
    """Yes/no with confidence (e.g. "is this addressed to the bot")."""

    answer: bool
    confidence: float


@dataclass(frozen=True)
class ScoreResult:
    value: float


@dataclass(frozen=True)
class TextResult:
    text: str
    input_tokens: int
    output_tokens: int
    # What the call cost, and how we know (ADR-0013): "reported" when
    # the provider returned it, "computed" when priced from tokens,
    # "unknown" when neither. None cost is recorded as unknown, never
    # as zero. See shared.observability.cost.
    cost: float | None = None
    cost_source: str = "unknown"


@dataclass(frozen=True)
class StructuredResult[OutputT]:
    """A parsed structured reply (`TextModelClient.generate_structured`)
    with the same usage fields as TextResult, so logging is uniform."""

    output: OutputT
    input_tokens: int
    output_tokens: int
    cost: float | None = None
    cost_source: str = "unknown"
