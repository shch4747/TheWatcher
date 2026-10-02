"""Jev assignment leaves confident existing-thread labels alone and hands
new-thread (and low-confidence) messages to the structured worker."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agents.wa_agent.assign import SenderNames
from agents.wa_agent.assign_jev import assign_with_jev
from agents.wa_agent.messages import BufferedMessage, ThreadInfo
from shared.models.schemas import ChoiceResult

from tests.scripted_models import ScriptedStructuredWorker

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
HALL = ThreadInfo(
    slug="20260101-hall", path="p", title="Booking the hall", summary="Hall for the demo.", state="active"
)


def _msg(message_id: str, text: str, minutes: int = 0) -> BufferedMessage:
    return BufferedMessage(
        row_id=0,
        message_id=message_id,
        channel="c@g.us",
        received_at=NOW + timedelta(minutes=minutes),
        text=text,
        sender="919000000001@s.whatsapp.net",
        sender_name="Aira",
    )


class _Decider:
    def __init__(self, choices: dict[str, ChoiceResult]):
        self.choices = choices
        self.calls = 0

    async def choice_batch(self, state: str, questions: dict) -> dict[str, ChoiceResult]:
        self.calls += 1
        return {name: self.choices[name] for name in questions}


async def _no_reply(_src_id: str) -> ThreadInfo | None:
    return None


async def test_confident_existing_thread_does_not_call_the_worker():
    decider = _Decider({"m1": ChoiceResult(option=HALL.slug, probabilities={HALL.slug: 0.9})})
    worker = ScriptedStructuredWorker()
    result = await assign_with_jev(
        [_msg("m1", "is the hall free")],
        [HALL],
        worker,
        SenderNames(),
        "Exes",
        "exes",
        None,
        _no_reply,
        decider,
    )
    assert result.buckets[HALL.slug][0].message_id == "m1"
    assert worker.assignment_prompts == []
    assert decider.calls == 1


async def test_new_thread_is_passed_to_the_worker():
    decider = _Decider({"m2": ChoiceResult(option="new-thread", probabilities={"new-thread": 0.95})})
    worker = ScriptedStructuredWorker(default_route="new-1", title="Venue deposit")
    result = await assign_with_jev(
        [_msg("m2", "we still owe the venue deposit")],
        [HALL],
        worker,
        SenderNames(),
        "Exes",
        "exes",
        None,
        _no_reply,
        decider,
    )
    assert worker.assignment_prompts
    assert any(key.startswith("new-thread:") for key in result.buckets)
    assert result.new_threads
