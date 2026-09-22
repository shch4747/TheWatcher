"""Structured assignment (ADR-0012): the prompt the model is given, the
token-budget chunking around it, the validation/retry loop, and the
deterministic rendering of items it returns. No live model calls - the
model is either scripted (tests.scripted_models) or pydantic-ai's
TestModel for the plumbing smoke test."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from agents.wa_agent.assign import (
    ASSIGN_SYSTEM_PROMPT,
    Assignment,
    AssignmentResponse,
    ItemOut,
    NewThread,
    SenderNames,
    ThreadUpdate,
    assign_batch,
    build_assignment_prompt,
    build_thread_update_prompt,
    chunk_messages,
    render_items,
    update_thread,
)
from agents.wa_agent.messages import BufferedMessage, ThreadInfo
from shared.models.schemas import StructuredResult
from shared.models.text import TextModelClient
from shared.wiki.interface import parse_item_line, parse_items

from tests.scripted_models import ScriptedStructuredWorker, prompt_message_ids

NOW = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)


def _msg(
    message_id: str,
    text: str,
    sender: str = "919000000001@s.whatsapp.net",
    minutes: int = 0,
    sender_name: str | None = None,
    replied_to_id: str | None = None,
) -> BufferedMessage:
    return BufferedMessage(
        row_id=0,
        message_id=message_id,
        channel="c@g.us",
        received_at=NOW + timedelta(minutes=minutes),
        text=text,
        sender=sender,
        sender_name=sender_name,
        replied_to_id=replied_to_id,
    )


def _names(**members: str | None) -> SenderNames:
    return SenderNames(members=dict(members))


async def _no_reply_target(_src_id: str) -> ThreadInfo | None:
    return None


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------


def test_assignment_prompt_lists_threads_messages_and_the_next_new_id():
    threads = [
        ThreadInfo(
            slug="20260101-hall",
            path="p",
            title="Booking the hall",
            summary="Hall availability for the demo.",
            state="active",
            recent_context="- 2026-01-01 10:00 — Aira: can we book it",
        ),
        ThreadInfo(slug="20260102-old", path="p", title="Old topic", summary="", state="stale"),
    ]
    names = _names(**{"919000000001@s.whatsapp.net": "Aira"})
    chunk = [_msg("m1", "any news on the hall?", sender_name="Aira J")]
    prompt = build_assignment_prompt(
        "Watcher", "project", "Watcher", threads, [], chunk, 3, names
    )

    assert "Channel: Watcher (kind: project, initiative: Watcher)" in prompt
    assert "### 20260101-hall [active]" in prompt
    assert "Description: Hall availability for the demo." in prompt
    assert "### 20260102-old [stale]" in prompt
    assert "New threads are numbered from new-3." in prompt
    # the registry title wins over the WhatsApp display name, and the
    # jid is kept alongside it (ADR-0012: no PII stripping)
    assert "- id=m1 | 2026-09-18 10:00 | Aira (919000000001@s.whatsapp.net) | any news on the hall?" in prompt


def test_assignment_prompt_falls_back_to_display_name_then_jid():
    names = _names(**{"919000000001@s.whatsapp.net": None, "919000000002@s.whatsapp.net": None})
    chunk = [
        _msg("m1", "hi", sender_name="Rohan"),
        _msg("m2", "yo", sender="919000000002@s.whatsapp.net", minutes=1),
    ]
    prompt = build_assignment_prompt("C", "other", None, [], [], chunk, 1, names)
    assert "Rohan (919000000001@s.whatsapp.net)" in prompt
    assert "| 919000000002@s.whatsapp.net | yo" in prompt


def test_assignment_prompt_shows_replies_and_already_assigned_as_context_only():
    names = _names(**{"a@x": None})
    chunk = [_msg("m2", "new topic", sender="a@x", minutes=2)]
    already = [(_msg("m1", "yes agreed", sender="a@x", replied_to_id="orig"), "20260101-hall")]
    followers = {"m2": [_msg("m3", "sounds good", sender="a@x", minutes=3, replied_to_id="m2")]}
    prompt = build_assignment_prompt(
        "C", "other", None, [], already, chunk, 1, names, followers=followers
    )
    assert "## Already assigned this batch" in prompt
    assert "-> 20260101-hall" in prompt
    assert "## Replies within this batch" in prompt
    assert "(in reply to m2)" in prompt
    # only the one real message is up for assignment
    assert prompt_message_ids(prompt) == ["m2"]


def test_assignment_prompt_keeps_a_multi_line_message_on_one_line():
    """Concatenated same-sender runs are multi-line; every prompt bullet
    must still start with `- id=` or the id list is ambiguous."""
    names = _names(**{"a@x": None})
    prompt = build_assignment_prompt(
        "C", "other", None, [], [], [_msg("m1", "book the hall\nfor the demo", sender="a@x")], 1, names
    )
    assert "- id=m1 | 2026-09-18 10:00 | a@x | book the hall ⏎ for the demo" in prompt
    assert prompt_message_ids(prompt) == ["m1"]


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------


def test_chunk_messages_splits_on_the_token_budget():
    names = _names(**{"a@x": None})
    messages = [_msg(f"m{i}", "a message of some length", sender="a@x", minutes=i) for i in range(10)]
    per_message = 20  # ~ estimate_tokens of one line

    chunks = chunk_messages(messages, names, budget_tokens=per_message * 3, max_messages=100)
    assert len(chunks) > 1
    assert sum(len(c) for c in chunks) == 10  # nothing dropped
    assert [m.message_id for c in chunks for m in c] == [m.message_id for m in messages]  # order kept


def test_chunk_messages_respects_the_message_count_cap():
    names = _names(**{"a@x": None})
    messages = [_msg(f"m{i}", "x", sender="a@x", minutes=i) for i in range(7)]
    chunks = chunk_messages(messages, names, budget_tokens=10_000, max_messages=3)
    assert [len(c) for c in chunks] == [3, 3, 1]


def test_chunk_messages_never_drops_a_message_bigger_than_the_budget():
    names = _names(**{"a@x": None})
    messages = [_msg("big", "x" * 5000, sender="a@x"), _msg("small", "ok", sender="a@x", minutes=1)]
    chunks = chunk_messages(messages, names, budget_tokens=10, max_messages=100)
    assert [m.message_id for c in chunks for m in c] == ["big", "small"]
    assert len(chunks[0]) == 1


# --------------------------------------------------------------------------
# assign_batch
# --------------------------------------------------------------------------


async def test_assign_batch_carries_new_threads_into_the_next_chunk(monkeypatch: pytest.MonkeyPatch):
    """A topic that spans a chunk boundary must land in one thread: the
    thread minted by chunk 1 is shown to chunk 2 as an existing thread,
    and new-N numbering continues instead of restarting."""
    from shared.config import settings

    monkeypatch.setattr(settings, "ingest_max_messages_per_call", 2)
    names = _names(**{"a@x": None})
    messages = [_msg(f"m{i}", f"message {i}", sender="a@x", minutes=i) for i in range(4)]

    worker = ScriptedStructuredWorker(
        route={"m0": "new-1", "m1": "new-1", "m2": "new-1", "m3": "new-2"}
    )
    result = await assign_batch(
        messages, [], worker, names, "C", "other", None, _no_reply_target
    )

    assert len(worker.assignment_prompts) == 2
    second = worker.assignment_prompts[1]
    assert "### new-1 [active]" in second  # chunk 1's thread is context for chunk 2
    assert "message 0" in second  # ... with its own messages as Recent
    assert "New threads are numbered from new-2." in second

    assert sorted(result.buckets) == ["new-thread:1", "new-thread:2"]
    assert [m.message_id for m in result.buckets["new-thread:1"]] == ["m0", "m1", "m2"]
    assert result.model_calls == 2
    assert result.new_threads["new-thread:1"].title == "Booking the seminar hall"


async def test_assign_batch_routes_replies_without_asking_and_keeps_batch_order():
    thread = ThreadInfo(slug="existing", path="p", title="Existing", summary="", state="active")

    async def _find(src_id: str) -> ThreadInfo | None:
        return thread if src_id == "orig-1" else None

    names = _names(**{"a@x": None})
    messages = [
        _msg("m0", "a reply to something older", sender="a@x", replied_to_id="orig-1"),
        _msg("m1", "an unrelated new thing", sender="a@x", minutes=1),
        _msg("m2", "following up on my reply", sender="a@x", minutes=2, replied_to_id="m0"),
    ]
    worker = ScriptedStructuredWorker(route={"m1": "new-1"})
    result = await assign_batch(messages, [thread], worker, names, "C", "other", None, _find)

    # the reply and its own follower both land on the existing thread,
    # and only the one non-reply was ever sent to the model
    assert [m.message_id for m in result.buckets["existing"]] == ["m0", "m2"]
    assert prompt_message_ids(worker.assignment_prompts[0]) == ["m1"]


async def test_assign_batch_retries_with_the_problem_spelled_out_then_succeeds():
    class FlakyWorker(ScriptedStructuredWorker):
        def _assign(self, prompt: str) -> AssignmentResponse:
            ids = prompt_message_ids(prompt)
            if "## Problems with your previous answer" not in prompt:
                # first answer: an unknown slug and a missing id
                return AssignmentResponse(
                    assignments=[Assignment(message_id=ids[0], thread="no-such-thread")]
                )
            return AssignmentResponse(
                new_threads=[NewThread(id="new-1", title="A topic", description="d")],
                assignments=[Assignment(message_id=mid, thread="new-1") for mid in ids],
            )

    names = _names(**{"a@x": None})
    messages = [_msg("m0", "one", sender="a@x"), _msg("m1", "two", sender="a@x", minutes=1)]
    worker = FlakyWorker()
    result = await assign_batch(messages, [], worker, names, "C", "other", None, _no_reply_target)

    assert len(worker.assignment_prompts) == 2
    retry = worker.assignment_prompts[1]
    assert "is not an existing slug" in retry
    assert "no assignment for message id(s): m1" in retry
    assert [m.message_id for m in result.buckets["new-thread:1"]] == ["m0", "m1"]
    assert result.unassigned == 0
    assert result.model_calls == 2


async def test_assign_batch_drops_to_chatter_when_every_attempt_is_unusable():
    """The batch must still complete - an unassignable message is
    counted and dropped, never left to block the cursor forever."""

    class HopelessWorker(ScriptedStructuredWorker):
        def _assign(self, prompt: str) -> AssignmentResponse:
            return AssignmentResponse(
                assignments=[Assignment(message_id=mid, thread="nope") for mid in prompt_message_ids(prompt)]
            )

    names = _names(**{"a@x": None})
    messages = [_msg("m0", "one", sender="a@x")]
    worker = HopelessWorker()
    result = await assign_batch(messages, [], worker, names, "C", "other", None, _no_reply_target)

    assert result.buckets == {"chatter": messages}
    assert result.unassigned == 1
    assert len(worker.assignment_prompts) == 3  # first try + ASSIGN_MAX_RETRIES


async def test_assign_batch_survives_a_call_that_raises():
    class ExplodingWorker(ScriptedStructuredWorker):
        async def generate_structured(self, prompt, output_type, system=None):
            raise RuntimeError("provider is down")

    names = _names(**{"a@x": None})
    messages = [_msg("m0", "one", sender="a@x")]
    result = await assign_batch(
        messages, [], ExplodingWorker(), names, "C", "other", None, _no_reply_target
    )
    assert result.buckets == {"chatter": messages}
    assert result.unassigned == 1


# --------------------------------------------------------------------------
# Thread update
# --------------------------------------------------------------------------


def test_thread_update_prompt_shows_current_state_and_new_messages_with_sender_and_time():
    names = _names(**{"919000000001@s.whatsapp.net": "Aira"})
    items_body = (
        "<!-- watcher:managed -->\n"
        "- [ ] Book the seminar hall [kind:: task] [src:: old1] ^i-9f3c\n"
        "<!-- /watcher -->"
    )
    prompt = build_thread_update_prompt(
        "Booking the hall", "Old summary.", items_body, [_msg("m1", "done, booked it")], names
    )
    assert "Thread title: Booking the hall" in prompt
    assert "Old summary." in prompt
    assert "^i-9f3c" in prompt  # so the model can reuse the id
    assert "Members (the only names allowed in owner): Aira" in prompt
    assert "- 2026-09-18 10:00 — Aira (919000000001@s.whatsapp.net): done, booked it [src:: m1]" in prompt


async def test_update_thread_renders_items_in_the_grammar():
    worker = ScriptedStructuredWorker(
        items=[
            ItemOut(kind="task", text="Book the hall", src_ids=["m1"], owner="Aira", due="2026-09-24"),
            ItemOut(kind="decision", text="Friday it is", src_ids=["m1"]),
            ItemOut(kind="question", text="Projector fixed?", src_ids=["m1"], owner="Aira"),
            ItemOut(kind="resource", text="https://slides", src_ids=["m1"]),
        ]
    )
    names = _names(**{"919000000001@s.whatsapp.net": "Aira"})
    _update, rendered = await update_thread(
        worker, "T", "", "", [_msg("m1", "hi")], names, {"m1"}
    )
    items = parse_items(rendered)
    assert len(items) == 4
    assert items[0].fields == {
        "kind": "task", "owner": "[[Aira]]", "due": "2026-09-24", "src": "m1"
    }
    assert items[0].checked is False
    assert items[1].checked is True  # decisions are recorded done
    assert items[2].fields["by"] == "[[Aira]]"  # a question's asker is `by`, not `owner`
    assert items[3].checked is None  # a resource has no checkbox


def test_render_items_drops_ungrounded_and_invalid_fields():
    rendered = render_items(
        [
            ItemOut(kind="task", text="valid", src_ids=["m1", "not-real"], owner="Aira", due="Friday"),
            ItemOut(kind="task", text="no real source", src_ids=["not-real"]),
            ItemOut(kind="task", text="   ", src_ids=["m1"]),
            ItemOut(kind="task", text="unknown owner", src_ids=["m1"], owner="Somebody Else"),
        ],
        existing=[],
        valid_src_ids={"m1"},
        allowed_owners={"Aira"},
    )
    items = parse_items(rendered)
    assert [i.text for i in items] == ["valid", "unknown owner"]
    assert items[0].src_ids() == ["m1"]  # the fabricated id was dropped
    assert "due" not in items[0].fields  # "Friday" isn't ISO 8601
    assert "owner" not in items[1].fields


def test_render_items_reuses_ids_and_mints_stable_ones():
    existing = parse_items("- [ ] Book the hall [kind:: task] [src:: m0] ^i-keepme")
    first = render_items(
        [
            ItemOut(kind="task", text="Book the hall", src_ids=["m0"]),  # same text, no id echoed
            ItemOut(kind="decision", text="Friday it is", src_ids=["m1"]),
        ],
        existing=existing,
        valid_src_ids={"m0", "m1"},
        allowed_owners=set(),
    )
    ids = [parse_item_line(line).block_id for line in first.splitlines()]
    assert ids[0] == "i-keepme"  # identity survived even without the model echoing it
    assert ids[1] != "i-keepme"

    # minting is deterministic: the same item yields the same id next batch
    second = render_items(
        [ItemOut(kind="decision", text="Friday it is", src_ids=["m1"])],
        existing=[],
        valid_src_ids={"m1"},
        allowed_owners=set(),
    )
    assert parse_item_line(second).block_id == ids[1]


def test_render_items_keeps_two_same_text_items_distinct():
    rendered = render_items(
        [
            ItemOut(kind="task", text="Follow up", src_ids=["m1"]),
            ItemOut(kind="task", text="Follow up", src_ids=["m2"]),
        ],
        existing=[],
        valid_src_ids={"m1", "m2"},
        allowed_owners=set(),
    )
    ids = [parse_item_line(line).block_id for line in rendered.splitlines()]
    assert len(set(ids)) == 2  # a duplicate id would merge two real items


# --------------------------------------------------------------------------
# Structured-output plumbing (not the scripted double)
# --------------------------------------------------------------------------


async def test_generate_structured_parses_a_real_model_reply():
    """The PromptedOutput wiring itself: a model returning JSON text is
    parsed into the contract, and usage is reported for logging."""
    import json

    from pydantic_ai.models.test import TestModel

    reply = json.dumps(
        {
            "new_threads": [{"id": "new-1", "title": "Booking the hall", "description": "d"}],
            "assignments": [{"message_id": "m1", "thread": "new-1"}],
        }
    )
    client = TextModelClient(TestModel(custom_output_text=reply), model_name="fixture")
    result: StructuredResult[AssignmentResponse] = await client.generate_structured(
        "prompt", AssignmentResponse, system=ASSIGN_SYSTEM_PROMPT
    )
    assert result.output.assignments[0].thread == "new-1"
    assert result.output.new_threads[0].title == "Booking the hall"
    assert result.input_tokens > 0


async def test_generate_structured_logs_the_call():
    import json

    from pydantic_ai.models.test import TestModel
    from shared.db import ModelCall, get_session
    from shared.models.interface import generate_structured
    from sqlalchemy import select

    reply = json.dumps({"title": "T", "summary": "S", "items": []})
    client = TextModelClient(TestModel(custom_output_text=reply), model_name="structured-fixture")
    result = await generate_structured(client, "worker", "prompt", ThreadUpdate)
    assert result.output.title == "T"

    async with get_session() as session:
        rows = list(await session.scalars(
            select(ModelCall).where(ModelCall.model_name == "structured-fixture")
        ))
    assert len(rows) == 1
    assert rows[0].tier == "worker"


async def test_a_crowded_channel_shrinks_thread_context_instead_of_starving_the_messages(
    monkeypatch: pytest.MonkeyPatch,
):
    """A channel with many long-running threads must not degenerate into
    one call per message: the context is squeezed (recent lines, then
    descriptions, then whole threads) so the messages keep their share
    of the budget."""
    from shared.config import settings
    from shared.models.tokens import estimate_tokens

    monkeypatch.setattr(settings, "ingest_max_prompt_tokens", 4000)
    monkeypatch.setattr(settings, "ingest_max_messages_per_call", 40)

    threads = [
        ThreadInfo(
            slug=f"2026090{i % 9}-thread-{i}",
            path=f"channels/c/t{i}.md",
            title=f"Thread number {i}",
            summary="A long summary sentence about this thread. " * 6,
            state="stale" if i % 2 else "active",
            recent_context="\n".join(f"- 2026-09-18 10:0{j} — Someone: a recent line" for j in range(5)),
        )
        for i in range(40)
    ]
    names = _names(**{"a@x": None})
    messages = [
        _msg(f"m{i}", f"a message of realistic length {i}", sender="a@x", minutes=i) for i in range(30)
    ]

    worker = ScriptedStructuredWorker()
    result = await assign_batch(messages, threads, worker, names, "C", "project", None, _no_reply_target)

    assert len(worker.assignment_prompts) == 1  # not 30
    prompt = worker.assignment_prompts[0]
    assert len(prompt_message_ids(prompt)) == 30
    assert estimate_tokens(ASSIGN_SYSTEM_PROMPT + prompt) < 4000
    assert "Recent:" not in prompt  # squeezed out to make room
    assert "### 2026090" in prompt  # slugs and titles still there to assign against
    assert result.unassigned == 0


async def test_batch_minted_threads_are_never_squeezed_out_of_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
):
    """Dropping a thread this batch just created would split its own
    topic across two threads, so persisted threads go first."""
    from shared.config import settings

    monkeypatch.setattr(settings, "ingest_max_prompt_tokens", 900)
    monkeypatch.setattr(settings, "ingest_max_messages_per_call", 1)

    threads = [
        ThreadInfo(
            slug=f"20260101-thread-{i}",
            path=f"channels/c/t{i}.md",
            title=f"An existing thread number {i} with a fairly long title",
            summary="A long summary sentence about this thread. " * 6,
            state="active",
        )
        for i in range(20)
    ]
    names = _names(**{"a@x": None})
    messages = [_msg(f"m{i}", f"message {i}", sender="a@x", minutes=i) for i in range(3)]

    worker = ScriptedStructuredWorker()  # everything -> new-1
    result = await assign_batch(messages, threads, worker, names, "C", "project", None, _no_reply_target)

    assert len(worker.assignment_prompts) == 3
    later = worker.assignment_prompts[-1]
    assert "### new-1 " in later  # survived the squeeze
    assert later.count("### ") < len(threads) + 1  # persisted threads were dropped instead
    assert len(result.buckets["new-thread:1"]) == 3  # all three in one thread


def test_thread_update_prompt_tells_the_summariser_to_ignore_bot_messages():
    from agents.wa_agent.assign import THREAD_UPDATE_SYSTEM_PROMPT
    from shared.config import settings

    assert settings.bot_mention_name in THREAD_UPDATE_SYSTEM_PROMPT
    assert "never make an item out of one" in THREAD_UPDATE_SYSTEM_PROMPT
