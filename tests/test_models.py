"""Phase 4 Models package acceptance criteria (docs/Spec - Watcher v1.md,
"Models"): tiers are swappable, every call is logged with tokens/cost,
and tests run against recorded fixtures (Testing Decisions) - no live
model calls here."""
from __future__ import annotations

from pydantic_ai.models.test import TestModel
from shared.db import ModelCall, get_session
from shared.models.decision import FixtureDecisionModel
from shared.models.interface import (
    ChoiceResult,
    NoulResult,
    ScoreResult,
    TextModelClient,
    decide_with_fallback,
    generate,
)
from sqlalchemy import select


def _worker(text: str) -> TextModelClient:
    return TextModelClient(TestModel(custom_output_text=text), model_name="fixture-worker")


async def test_fixture_decision_model_choice_noul_score():
    fixture = FixtureDecisionModel(
        choices={"which thread?": ChoiceResult(option="thread-a", probabilities={"thread-a": 0.9})},
        nouls={"addressed to bot?": NoulResult(answer=True, confidence=0.95)},
        scores={"urgency": ScoreResult(value=0.4)},
    )
    choice = await fixture.choice("which thread?", ["thread-a", "thread-b"])
    assert choice.option == "thread-a"
    assert choice.confidence == 0.9

    noul = await fixture.noul("addressed to bot?")
    assert noul.answer is True

    score = await fixture.score("urgency")
    assert score.value == 0.4


async def test_fixture_decision_model_raises_on_unrecorded_question():
    fixture = FixtureDecisionModel()
    try:
        await fixture.choice("never recorded", ["a", "b"])
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for an unrecorded fixture question")


async def test_generate_logs_tokens_to_model_calls():
    worker = _worker("a short summary")
    result = await generate(worker, "worker", "summarize this thread")
    assert result.text == "a short summary"

    async with get_session() as session:
        rows = await session.scalars(select(ModelCall).where(ModelCall.model_name == "fixture-worker"))
    rows = list(rows)
    assert len(rows) == 1
    assert rows[0].tier == "worker"
    assert rows[0].input_tokens > 0


async def test_decide_with_fallback_uses_decision_model_above_threshold():
    fixture = FixtureDecisionModel(
        choices={"q": ChoiceResult(option="new-thread", probabilities={"new-thread": 0.9, "chatter": 0.1})}
    )
    worker = _worker("should not be called")
    result = await decide_with_fallback(fixture, worker, "q", ["new-thread", "chatter"])
    assert result.option == "new-thread"
    assert result.confidence == 0.9


async def test_decide_with_fallback_falls_back_to_worker_below_threshold():
    fixture = FixtureDecisionModel(
        choices={"q2": ChoiceResult(option="new-thread", probabilities={"new-thread": 0.4, "chatter": 0.3})}
    )
    worker = _worker("chatter")
    result = await decide_with_fallback(fixture, worker, "q2", ["new-thread", "chatter"])
    assert result.option == "chatter"
    assert result.probabilities["chatter"] == 1.0


async def test_decide_with_fallback_keeps_decision_if_worker_answer_invalid():
    fixture = FixtureDecisionModel(
        choices={"q3": ChoiceResult(option="new-thread", probabilities={"new-thread": 0.4, "chatter": 0.3})}
    )
    worker = _worker("something not in the option list")
    result = await decide_with_fallback(fixture, worker, "q3", ["new-thread", "chatter"])
    assert result.option == "new-thread"  # fell back to the Decision Model's own top pick
