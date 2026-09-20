"""Phase 4 Models package acceptance criteria (docs/Spec - Watcher v1.md,
"Models"): tiers are swappable, every call is logged with tokens/cost,
and tests run against recorded fixtures (Testing Decisions) - no live
model calls here."""
from __future__ import annotations

import pytest
from pydantic_ai.models.test import TestModel
from shared.db import ModelCall, get_session
from shared.models.decision import FixtureDecisionModel, WorkerBackedDecisionModel, default_decision_client
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


async def test_worker_backed_decision_model_choice_noul_score():
    decision = WorkerBackedDecisionModel(_worker("chatter"))
    choice = await decision.choice("which bucket?", ["new-thread", "chatter"])
    assert choice.option == "chatter"
    assert choice.confidence == 1.0

    yes_decision = WorkerBackedDecisionModel(_worker("Yes, that's right."))
    noul = await yes_decision.noul("is this an approval?")
    assert noul.answer is True

    no_decision = WorkerBackedDecisionModel(_worker("No."))
    noul_no = await no_decision.noul("is this an approval?")
    assert noul_no.answer is False

    score_decision = WorkerBackedDecisionModel(_worker("0.75"))
    score = await score_decision.score("how urgent is this?")
    assert score.value == 0.75


async def test_worker_backed_decision_model_falls_back_on_invalid_choice():
    decision = WorkerBackedDecisionModel(_worker("not one of the options"))
    choice = await decision.choice("which bucket?", ["new-thread", "chatter"])
    assert choice.option == "new-thread"  # first option, since the answer didn't match either


def test_default_decision_client_picks_worker_backed_without_jev(monkeypatch: pytest.MonkeyPatch):
    from shared.config import settings

    monkeypatch.setattr(settings, "jev_base_url", None)
    client = default_decision_client(_worker("x"))
    assert isinstance(client, WorkerBackedDecisionModel)
