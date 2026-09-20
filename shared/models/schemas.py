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
