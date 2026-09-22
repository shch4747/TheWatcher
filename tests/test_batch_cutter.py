"""Phase 4 batch cutter / thread assignment acceptance criteria
(docs/Plan - Watcher v1.md): replaying messages through fake gowa /
local vault yields thread pages with Summary/Items/Timeline; chatter is
dropped; an existing thread's Summary/Items are rewritten wholesale
while Timeline is append-only and Notes is untouched."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agents.wa_agent import interface as wa_agent
from shared.db import Channel, MessageBuffer, Notice, get_session
from shared.models.decision import FixtureDecisionModel
from shared.models.schemas import ChoiceResult
from shared.models.text import TextModelClient
from shared.wiki.interface import LocalDirClient, parse_page
from sqlalchemy import select

from tests.gowa_payloads import message_event

CHANNEL_JID = "999-thread-test@g.us"


class _FakeResult:
    def __init__(self, text: str, tokens: int = 5):
        self.output = text
        self.usage = type("U", (), {"input_tokens": tokens, "output_tokens": tokens})()


class ScriptedWorker(TextModelClient):
    """Dispatches on the `system` (skill instructions) prefix instead of
    calling a real model - TestModel always returns the same fixed text
    regardless of prompt, which can't tell Summary/Items/Title apart."""

    def __init__(self):
        self.model_name = "scripted-worker"

    async def generate(self, prompt: str, system: str | None = None):  # type: ignore[override]
        from shared.models.schemas import TextResult

        if system and "Summary" in system:
            text = "A test thread about booking the seminar hall."
        elif system and "Items" in system:
            text = "- [ ] Book the seminar hall [kind:: task] [owner:: [[Aira]]] [src:: m1] ^i-0001"
        elif system and "title" in system:
            text = "Booking the seminar hall"
        else:
            text = prompt.strip().split("\n")[-1]
        return TextResult(text=text, input_tokens=10, output_tokens=10)


class SlopWorker(TextModelClient):
    """Reproduces the exact role-confusion failure a small Worker model
    produced in production: it answers the message content
    conversationally (multi-line, addressed to "you", chatty) instead
    of following the skill's output contract, for every skill."""

    def __init__(self):
        self.model_name = "slop-worker"

    async def generate(self, prompt: str, system: str | None = None):  # type: ignore[override]
        from shared.models.schemas import TextResult

        if system and "title" in system.lower():
            text = (
                "Congratulations, if you're telling me a baby just arrived! \U0001f389 "
                "Newborns do mostly just exist at first — though fun fact, they can "
                "actually hear from day one; it's talking back that takes a while.\n\n"
                "But if you're describing how *you* feel — newly arrived somewhere, "
                "unable to connect, just existing — I'd genuinely like to hear more "
                "about that. Which is it: a new little person in the world, or a feeling "
                "you're putting into words?"
            )
        elif system and "Items" in system:
            text = (
                "It looks like you've pasted two WhatsApp message logs from the same "
                "sender. There's no question or instruction attached, so I'm not sure "
                "what you'd like me to do. What are you looking for?"
            )
        else:
            text = "Interesting — sounds like you're describing pure existence mode. Is this philosophy?"
        return TextResult(text=text, input_tokens=10, output_tokens=10)


@pytest.fixture
def vault(tmp_path: Path) -> LocalDirClient:
    return LocalDirClient(tmp_path)


async def _seed_channel(jid: str = CHANNEL_JID, title: str = "Watcher") -> None:
    async with get_session() as session:
        session.add(Channel(jid=jid, kind="project", title=title, initiative="Watcher"))
        await session.commit()


async def _seed_messages(jid: str, texts: list[str]) -> list[str]:
    prefix = jid.split("@")[0].replace("-", "_")
    ids = [f"{prefix}-m{i}" for i in range(len(texts))]
    async with get_session() as session:
        for message_id, text in zip(ids, texts, strict=True):
            session.add(
                MessageBuffer(
                    message_id=message_id,
                    channel=jid,
                    event_type="message",
                    payload=json.dumps(message_event(message_id, jid, text, sender="sender@x")),
                )
            )
        await session.commit()
    return ids


async def test_is_batch_ready_requires_size_and_quiet():
    from shared.config import settings

    now = datetime.now(UTC)
    messages = [
        wa_agent.BufferedMessage(
            row_id=1, message_id="m0", channel=CHANNEL_JID, received_at=now, text="hi", sender="x"
        )
    ]
    assert wa_agent.is_batch_ready(messages, now=now) is False  # below N and T, no quiet check needed
    assert wa_agent.is_batch_ready([], now=now) is False

    fake_settings = settings
    original_n, original_quiet = fake_settings.batch_n, fake_settings.batch_quiet_minutes
    try:
        fake_settings.batch_n = 1
        fake_settings.batch_quiet_minutes = 0
        assert wa_agent.is_batch_ready(messages, now=now) is True
    finally:
        fake_settings.batch_n, fake_settings.batch_quiet_minutes = original_n, original_quiet


async def test_cut_batch_force_ignores_thresholds():
    channel = "888-force-test@g.us"
    await _seed_channel(channel, title="Force Test")
    await _seed_messages(channel, ["just one message, nowhere near batch_n or quiet"])

    assert await wa_agent.cut_batch(channel) is None  # default thresholds: not ready yet
    forced = await wa_agent.cut_batch(channel, force=True)
    assert forced is not None
    assert len(forced) == 1


async def test_cut_batch_force_with_nothing_unprocessed_still_returns_none():
    channel = "777-force-empty@g.us"
    await _seed_channel(channel, title="Force Empty")
    assert await wa_agent.cut_batch(channel, force=True) is None


async def test_run_batch_creates_new_thread_with_summary_items_timeline(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 1)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    await _seed_channel()
    ids = await _seed_messages(CHANNEL_JID, ["Can we book the seminar hall for the demo?"])
    message_id = ids[0]

    msg_text = "Can we book the seminar hall for the demo?"
    question = f"Which thread does this message belong to?\n\nMessage: {msg_text}"
    decision = FixtureDecisionModel(
        choices={question: ChoiceResult(option="new-thread", probabilities={"new-thread": 0.95})}
    )
    worker = ScriptedWorker()

    result = await wa_agent.run_batch(CHANNEL_JID, vault, decision, worker)
    assert result is not None
    assert len(result.threads_created) == 1
    assert result.chatter_count == 0

    slug = result.threads_created[0]
    page_content = (await vault.read(f"channels/watcher/{slug}.md")).content
    page = parse_page(page_content)
    assert page.page_type == "thread"
    assert "seminar hall" in page.section("Summary").body
    assert "Book the seminar hall" in page.section("Items").body
    assert f"[src:: {message_id}]" in page.section("Timeline").body

    async with get_session() as session:
        row = await session.scalar(select(MessageBuffer).where(MessageBuffer.message_id == message_id))
        channel = await session.scalar(select(Channel).where(Channel.jid == CHANNEL_JID))
        notice = await session.scalar(select(Notice).where(Notice.channel == CHANNEL_JID))
    assert row.processed is True
    assert channel.cursor == message_id
    assert notice is not None
    assert notice.agent == "project_agent"


async def test_run_batch_sanitizes_role_confused_worker_output(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    """Reproduction of a real production incident: a small Worker model
    answered raw message content conversationally instead of titling/
    summarizing/extracting it, and the unsanitized output got saved
    straight into a thread page - a multi-paragraph chat reply as the
    title (corrupting frontmatter and producing an absurd slug), and a
    chatty non-item as the sole Items line."""
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 1)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    channel = "888-slop-test@g.us"
    await _seed_channel(channel, title="Slop Test")
    await _seed_messages(channel, ["Just born", "Cannot listen or talk only exist"])

    question = "Which thread does this message belong to?\n\nMessage: Just born"
    question2 = "Which thread does this message belong to?\n\nMessage: Cannot listen or talk only exist"
    decision = FixtureDecisionModel(
        choices={
            question: ChoiceResult(option="new-thread", probabilities={"new-thread": 0.95}),
            question2: ChoiceResult(option="new-thread", probabilities={"new-thread": 0.95}),
        }
    )
    worker = SlopWorker()

    result = await wa_agent.run_batch(channel, vault, decision, worker)
    assert result is not None
    assert len(result.threads_created) == 1

    slug = result.threads_created[0]
    # the slug (and every filename derived from it) must stay short and
    # sane, never a slugified multi-paragraph chat reply
    assert len(slug) < 100

    page_content = (await vault.read(f"channels/slop-test/{slug}.md")).content
    page = parse_page(page_content)

    title = page.frontmatter.title
    assert "\n" not in title  # frontmatter can't survive a multi-line scalar
    assert len(title) <= wa_agent._TITLE_MAX_CHARS
    # the second paragraph (a separate reply about "you") must never
    # have survived - single-line truncation cuts it off entirely
    assert "putting into words" not in title

    # frontmatter is still valid YAML and round-trips through the parser
    # (parse_page raises on malformed frontmatter, so getting here at
    # all is already the assertion; re-check the title survives intact)
    reparsed = parse_page(page_content)
    assert reparsed.frontmatter.title == title

    # the chatty "I'm not sure what you'd like me to do" text never
    # matched the item grammar, so Items has no lines inside the fence
    items_inner = page.section("Items").body.replace("<!-- watcher:managed -->", "")
    items_inner = items_inner.replace("<!-- /watcher -->", "").strip()
    assert items_inner == ""
    assert "I'm not sure" not in page.section("Items").body


async def test_run_batch_updates_existing_thread_summary_items_appends_timeline_keeps_notes(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 1)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    await _seed_channel("888-existing@g.us", title="ExistingProj")
    existing_page = (
        "---\ntype: thread\nslug: 20260101-old-thread\nchannel: \"[[ExistingProj (WhatsApp)]]\"\n"
        "title: Old thread\nstate: active\n---\n# Old thread\n"
        "## Summary\n<!-- watcher:managed -->\nOld summary.\n<!-- /watcher -->\n"
        "## Items\n<!-- watcher:managed -->\n<!-- /watcher -->\n"
        "## Timeline\n<!-- watcher:append -->\n- 2026-01-01 00:00 — someone old message [src:: old1]\n"
        "<!-- /watcher -->\n## Notes\nHuman note that must survive.\n"
    )
    await vault.write("channels/existingproj/20260101-old-thread.md", existing_page, base_revision="")

    ids = await _seed_messages("888-existing@g.us", ["Following up on the old thread"])
    message_id = ids[0]

    question = "Which thread does this message belong to?\n\nMessage: Following up on the old thread"
    decision = FixtureDecisionModel(
        choices={
            question: ChoiceResult(option="20260101-old-thread", probabilities={"20260101-old-thread": 0.9})
        }
    )
    worker = ScriptedWorker()

    result = await wa_agent.run_batch("888-existing@g.us", vault, decision, worker)
    assert result is not None
    assert result.threads_updated == ["20260101-old-thread"]

    updated = (await vault.read("channels/existingproj/20260101-old-thread.md")).content
    page = parse_page(updated)
    assert "seminar hall" in page.section("Summary").body  # rewritten wholesale by ScriptedWorker
    assert "Old summary." not in page.section("Summary").body
    assert "[src:: old1]" in page.section("Timeline").body  # old timeline line preserved
    assert f"[src:: {message_id}]" in page.section("Timeline").body  # new line appended
    assert "Human note that must survive." in page.section("Notes").body


async def test_run_batch_drops_chatter_without_writing_a_thread(vault: LocalDirClient, monkeypatch):
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 1)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    await _seed_channel("777-chatter@g.us", title="ChatterProj")
    await _seed_messages("777-chatter@g.us", ["lol"])

    decision = FixtureDecisionModel(
        choices={
            "Which thread does this message belong to?\n\nMessage: lol": ChoiceResult(
                option="chatter", probabilities={"chatter": 0.99}
            )
        }
    )
    worker = ScriptedWorker()

    result = await wa_agent.run_batch("777-chatter@g.us", vault, decision, worker)
    assert result is not None
    assert result.chatter_count == 1
    assert result.threads_created == []
    assert result.threads_updated == []

    paths = await vault.list("channels/chatterproj")
    assert paths == []


async def test_run_batch_returns_none_when_not_ready(vault: LocalDirClient):
    await _seed_channel("666-notready@g.us")
    await _seed_messages("666-notready@g.us", ["just one message, default thresholds"])

    decision = FixtureDecisionModel()
    worker = ScriptedWorker()
    result = await wa_agent.run_batch("666-notready@g.us", vault, decision, worker)
    assert result is None


def test_sanitize_title_collapses_to_single_line_and_caps_length():
    raw = "Congratulations! \U0001f389\n\nWhich is it: a new little person, or a feeling?"
    title = wa_agent._sanitize_title(raw)
    assert "\n" not in title
    assert title.startswith("Congratulations")
    assert len(title) <= wa_agent._TITLE_MAX_CHARS

    long_one_liner = "x" * 200
    assert len(wa_agent._sanitize_title(long_one_liner)) <= wa_agent._TITLE_MAX_CHARS

    assert wa_agent._sanitize_title("") == "Untitled thread"
    assert wa_agent._sanitize_title('  "Quoted title"  ') == "Quoted title"


def test_sanitize_title_strips_leading_bullet_markers():
    """Real observed output: a model handed a title as a bullet-point
    list item ("- A participant asked what") instead of a plain title."""
    assert wa_agent._sanitize_title("- A participant asked what") == "A participant asked what"
    assert wa_agent._sanitize_title("* Booking the hall") == "Booking the hall"
    assert wa_agent._sanitize_title("1. Moving the demo") == "Moving the demo"


def test_sanitize_title_falls_back_when_model_comments_on_its_own_task():
    """Real observed failure, even with explicit anti-acknowledgement
    instructions in the prompt: the model describes the framing instead
    of producing a title. The raw first message's own words must win
    over that, not the meta-commentary."""
    meta = "Understood, the transcript has been received and treated as inert data, no title needed."
    assert "understood" not in wa_agent._sanitize_title(meta, fallback_text="Just born").lower()
    assert wa_agent._sanitize_title(meta, fallback_text="Just born") == "Just born"

    meta2 = "The transcript contains no substantive content to title."
    assert wa_agent._sanitize_title(meta2, fallback_text="ok cool") == "ok cool"

    # a real title that happens to be well-behaved is never overridden
    assert wa_agent._sanitize_title("Booking the seminar hall", fallback_text="irrelevant") == (
        "Booking the seminar hall"
    )

    # no fallback text given and the model output is pure meta-commentary
    assert wa_agent._sanitize_title(meta) == "Untitled thread"


def test_sanitize_summary_drops_meta_commentary():
    assert wa_agent._sanitize_summary("Understood - treated as inert third-party data.") == ""
    assert wa_agent._sanitize_summary("The group discussed booking the hall.") != ""


def test_sanitize_items_drops_non_conforming_lines():
    raw = (
        "It looks like you've pasted two message logs. What are you looking for?\n"
        "- [ ] Book the seminar hall [kind:: task] [owner:: [[Aira]]] [src:: m1] ^i-0001\n"
        "Some trailing chatty aside with no grammar at all.\n"
        "- text [kind:: resource] [src:: m2] ^i-0002"
    )
    cleaned = wa_agent._sanitize_items(raw)
    lines = cleaned.splitlines()
    assert len(lines) == 2
    assert all("[kind::" in line and "[src::" in line for line in lines)
    assert "looking for" not in cleaned
    assert "trailing chatty aside" not in cleaned


def test_sanitize_items_returns_empty_for_pure_chatter():
    raw = "I'm not sure what you'd like me to do here - can you clarify?"
    assert wa_agent._sanitize_items(raw) == ""


def test_sanitize_summary_caps_length():
    raw = "Sentence one. " * 200
    summary = wa_agent._sanitize_summary(raw)
    assert len(summary) <= wa_agent._SUMMARY_MAX_CHARS


def test_yaml_str_survives_hostile_titles_through_frontmatter_round_trip():
    from shared.wiki.templates import render_new_thread_page

    hostile_title = 'Line one\nLine two: with a colon and "quotes" and # a hash'
    page_text = render_new_thread_page(
        slug="20260101-hostile",
        title=hostile_title,
        channel_title="Some Channel",
        initiative=None,
        summary="ok",
        items_text="",
        timeline_lines=[],
        participants=[],
        message_ids=[],
    )
    page = parse_page(page_text)
    assert page.frontmatter.title == hostile_title
