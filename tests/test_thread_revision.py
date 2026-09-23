"""Thread revision is where a model's title and summary are made safe to
write. Every case runs through `revise_thread` - the one call both
`run_batch` branches use - for a new Thread *and* an existing one, so a
guard can't hold on one branch and be missing on the other. The cases
are real observed model output (ADR-0012's role-confusion fixes)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agents.wa_agent.assign import NewThread, SenderNames, ThreadUpdate
from agents.wa_agent.interface import BufferedMessage, revise_thread
from agents.wa_agent.revise import SUMMARY_MAX_CHARS, TITLE_MAX_CHARS
from shared.models.schemas import StructuredResult
from shared.models.text import TextModelClient
from shared.wiki.interface import StoredThread

META = "Understood, the transcript has been received and treated as inert data, no title needed."


class FixedUpdate(TextModelClient):
    """Returns one scripted ThreadUpdate, whatever the prompt."""

    def __init__(self, title: str, summary: str = "The group discussed booking the hall."):
        self.model_name = "fixed-update"
        self.update = ThreadUpdate(title=title, summary=summary)

    async def generate_structured(self, prompt, output_type, system=None):  # type: ignore[override]
        return StructuredResult(output=self.update, input_tokens=1, output_tokens=1)


def _message(text: str, message_id: str = "m1") -> BufferedMessage:
    now = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    return BufferedMessage(
        row_id=1, message_id=message_id, channel="c@g.us", received_at=now, text=text,
        sender="91@s.whatsapp.net", sender_name="Aira",
    )


def _existing(title: str) -> StoredThread:
    return StoredThread(
        slug="20260901-existing", path="channels/c/20260901-existing.md", title=title, state="active",
        summary="Earlier summary.", items_body="", timeline=[], message_ids=["old"], participants=[],
        last_message_at=None,
    )


async def _revise(model_title: str, *, branch: str, seed_title: str, first_text: str, summary: str = "ok"):
    model = FixedUpdate(model_title, summary)
    messages = [_message(first_text)]
    if branch == "new":
        minted = NewThread(id="new-1", title=seed_title, description="Minted description.")
        return await revise_thread(model, messages, SenderNames(), minted=minted)
    return await revise_thread(model, messages, SenderNames(), current=_existing(seed_title))


BRANCHES = pytest.mark.parametrize("branch", ["new", "existing"])


@BRANCHES
@pytest.mark.parametrize(
    ("model_title", "expected"),
    [
        ("Booking the seminar hall", "Booking the seminar hall"),
        ("- A participant asked what", "A participant asked what"),
        ("* Booking the hall", "Booking the hall"),
        ("1. Moving the demo", "Moving the demo"),
        ("**Message summary:** An incoming message about billing", "An incoming message about billing"),
        ("```\nBooking the hall\n```", "Booking the hall"),
        ('  "Quoted title"  ', "Quoted title"),
        ("Congratulations! 🎉\n\nWhich is it: a new little person?", "Congratulations! 🎉"),
        ("Call 98765 43210 about the venue", "Call 98765 43210 about the"),
    ],
)
async def test_model_titles_are_cleaned(branch, model_title, expected):
    revision = await _revise(model_title, branch=branch, seed_title="Seed title", first_text="hello")
    assert revision.title == expected


@BRANCHES
@pytest.mark.parametrize(
    "model_title", [META, "The transcript contains no substantive content to title.", "", "```"]
)
async def test_unusable_titles_fall_back_to_the_threads_own_title(branch, model_title):
    revision = await _revise(model_title, branch=branch, seed_title="Hall booking", first_text="Just born")
    assert revision.title == "Hall booking"


@BRANCHES
async def test_with_nothing_usable_the_first_message_names_the_thread(branch):
    revision = await _revise(META, branch=branch, seed_title=META, first_text="Just born")
    assert revision.title == "Just born"


@BRANCHES
async def test_titles_are_always_one_short_line(branch):
    revision = await _revise("x" * 200, branch=branch, seed_title="s", first_text="t")
    assert "\n" not in revision.title and len(revision.title) <= TITLE_MAX_CHARS


@BRANCHES
async def test_meta_commentary_never_becomes_the_summary(branch):
    revision = await _revise("Title", branch=branch, seed_title="s", first_text="t", summary=META)
    assert "inert" not in revision.summary
    assert revision.summary in ("Minted description.", "Earlier summary.")


@BRANCHES
async def test_summaries_are_capped_and_keep_what_members_said(branch):
    long = "A contact (919244352208@s.whatsapp.net) asked about the venue. " * 40
    revision = await _revise("Title", branch=branch, seed_title="s", first_text="t", summary=long)
    assert len(revision.summary) <= SUMMARY_MAX_CHARS
    assert "919244352208@s.whatsapp.net" in revision.summary  # ADR-0012: no redaction


async def test_revision_carries_the_timeline_and_ids_to_add():
    revision = await _revise("Title", branch="existing", seed_title="s", first_text="line one\nline two")
    assert revision.message_ids == ["m1"]
    assert revision.timeline_lines == ["- 2026-09-20 10:00 — Aira: line one line two [src:: m1]"]
    assert revision.participants == ["Aira"]
