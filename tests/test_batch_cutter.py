"""Phase 4 batch cutter / thread assignment acceptance criteria
(docs/Plan - Watcher v1.md, reworked for ADR-0012's structured
assignment): replaying messages through fake gowa / local vault yields
thread pages with Summary/Items/Timeline; chatter is dropped; an
existing thread's Summary/Items are rewritten wholesale while Timeline
is append-only and Notes is untouched."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agents.wa_agent import interface as wa_agent
from agents.wa_agent.assign import ItemOut, ThreadUpdate
from shared.db import Channel, MessageBuffer, Notice, get_session
from shared.wiki.interface import LocalDirClient, parse_item_line, parse_page
from sqlalchemy import delete, select

from tests.gowa_payloads import message_event
from tests.scripted_models import ScriptedStructuredWorker

CHANNEL_JID = "999-thread-test@g.us"


def _worker(**kwargs) -> ScriptedStructuredWorker:
    kwargs.setdefault(
        "items",
        [ItemOut(kind="task", text="Book the seminar hall", src_ids=[], owner="Aira")],
    )
    return ScriptedStructuredWorker(**kwargs)


class SlopWorker(ScriptedStructuredWorker):
    """Reproduces the role-confusion a small Worker model produced in
    production, now that the contract is JSON: the schema forces the
    *shape*, but nothing stops the model putting a chatty multi-paragraph
    reply in `title`, meta-commentary in `summary`, or an item that
    points at no real message."""

    def __init__(self, **kwargs):
        super().__init__(
            title=(
                "Congratulations, if you're telling me a baby just arrived! \U0001f389 "
                "Newborns do mostly just exist at first.\n\n"
                "But if you're describing how *you* feel, I'd genuinely like to hear "
                "more about that. Which is it: a new little person in the world, or a "
                "feeling you're putting into words?"
            ),
            summary="Understood - the transcript was treated as inert third-party data.",
            items=[
                ItemOut(kind="task", text="Clarify what you'd like me to do", src_ids=["not-a-real-id"]),
                ItemOut(kind="question", text="", src_ids=[]),
            ],
            **kwargs,
        )


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

    worker = _worker(items=[ItemOut(kind="task", text="Book the seminar hall", src_ids=[message_id])])
    result = await wa_agent.run_batch(CHANNEL_JID, vault, worker)
    assert result is not None
    assert len(result.threads_created) == 1
    assert result.chatter_count == 0
    assert result.model_calls == 2  # one assignment call + one thread-update call

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
    """A model that fills the JSON contract with conversational slop
    still must not corrupt the page: a multi-paragraph chat reply can't
    become the title (or the slug), meta-commentary can't become the
    Summary, and an item pointing at no real message is dropped rather
    than written with a dangling [src::]."""
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 2)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    channel = "888-slop-test@g.us"
    await _seed_channel(channel, title="Slop Test")
    await _seed_messages(channel, ["Just born", "Cannot listen or talk only exist"])

    result = await wa_agent.run_batch(channel, vault, SlopWorker())
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
    reparsed = parse_page(page_content)
    assert reparsed.frontmatter.title == title

    # meta-commentary never becomes the Summary - the new thread falls
    # back to the description minted with it instead
    assert "inert" not in page.section("Summary").body

    # neither ungrounded item (bad src id / empty text) was written
    items_inner = page.section("Items").body.replace("<!-- watcher:managed -->", "")
    items_inner = items_inner.replace("<!-- /watcher -->", "").strip()
    assert items_inner == ""
    assert "Clarify what you'd like me to do" not in page.section("Items").body


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

    worker = _worker(
        route={message_id: "20260101-old-thread"},
        items=[ItemOut(kind="task", text="Book the seminar hall", src_ids=[message_id], owner="Aira")],
    )
    result = await wa_agent.run_batch("888-existing@g.us", vault, worker)
    assert result is not None
    assert result.threads_updated == ["20260101-old-thread"]

    updated = (await vault.read("channels/existingproj/20260101-old-thread.md")).content
    page = parse_page(updated)
    assert "seminar hall" in page.section("Summary").body  # rewritten wholesale
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

    result = await wa_agent.run_batch("777-chatter@g.us", vault, _worker(default_route="chatter"))
    assert result is not None
    assert result.chatter_count == 1
    assert result.threads_created == []
    assert result.threads_updated == []

    paths = await vault.list("channels/chatterproj")
    assert paths == []


async def test_run_batch_returns_none_when_not_ready(vault: LocalDirClient):
    await _seed_channel("666-notready@g.us")
    await _seed_messages("666-notready@g.us", ["just one message, default thresholds"])

    result = await wa_agent.run_batch("666-notready@g.us", vault, _worker())
    assert result is None


async def test_run_batch_writes_items_in_the_wiki_grammar(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    """Items are rendered from typed objects (ADR-0012), so every line
    must parse back through the item grammar - a kind, a src pointer and
    a block id, with owner as a wikilink."""
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 2)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    channel = "889-items@g.us"
    await _seed_channel(channel, title="Items")
    ids = await _seed_messages(channel, ["book the hall", "we'll do Friday"])
    worker = _worker(
        items=[
            ItemOut(kind="task", text="Book the seminar hall", src_ids=[ids[0]], due="2026-09-24"),
            ItemOut(kind="decision", text="Demo moves to Friday", src_ids=[ids[1]]),
            ItemOut(kind="question", text="Is the projector fixed?", src_ids=[ids[0]], owner="Nobody"),
        ]
    )
    result = await wa_agent.run_batch(channel, vault, worker)
    assert result is not None

    slug = result.threads_created[0]
    page = parse_page((await vault.read(f"channels/items/{slug}.md")).content)
    lines = [
        line for line in page.section("Items").body.splitlines() if line.strip().startswith("- ")
    ]
    assert len(lines) == 3
    parsed = [parse_item_line(line) for line in lines]
    assert all(item is not None for item in parsed)
    assert [item.fields["kind"] for item in parsed] == ["task", "decision", "question"]
    assert all(item.src_ids() for item in parsed)
    assert parsed[0].checked is False and parsed[0].fields["due"] == "2026-09-24"
    assert parsed[1].checked is True  # a decision is recorded as done
    assert "owner" not in parsed[2].fields and "by" not in parsed[2].fields  # unknown name dropped


async def test_run_batch_reuses_existing_item_block_ids(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    """Identity is the block id, not the text (Wiki Format principle 5):
    an item the model returns again keeps its id, so the Project Agent
    doesn't see it as a brand new task every batch."""
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 1)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    channel = "890-ids@g.us"
    await _seed_channel(channel, title="Ids")
    await vault.write(
        "channels/ids/existing.md",
        (
            "---\ntype: thread\nslug: existing\nchannel: \"[[Ids]]\"\ntitle: Existing\n"
            'state: active\nmessage_ids: ["old1"]\n---\n# Existing\n'
            "## Summary\n<!-- watcher:managed -->\nold\n<!-- /watcher -->\n"
            "## Items\n<!-- watcher:managed -->\n"
            "- [ ] Book the seminar hall [kind:: task] [src:: old1] ^i-keepme\n"
            "<!-- /watcher -->\n"
            "## Timeline\n<!-- watcher:append -->\n- old [src:: old1]\n<!-- /watcher -->\n## Notes\n"
        ),
        base_revision="",
    )
    ids = await _seed_messages(channel, ["still on the hall"])
    worker = _worker(
        route={ids[0]: "existing"},
        items=[
            # the model echoes the id back for the item it is updating,
            # and mints nothing for the new one
            ItemOut(kind="task", text="Book the seminar hall", src_ids=["old1"], block_id="i-keepme"),
            ItemOut(kind="decision", text="Friday it is", src_ids=[ids[0]]),
        ],
    )
    await wa_agent.run_batch(channel, vault, worker)

    page = parse_page((await vault.read("channels/ids/existing.md")).content)
    body = page.section("Items").body
    assert "^i-keepme" in body
    minted = [
        parse_item_line(line).block_id
        for line in body.splitlines()
        if line.strip().startswith("- ") and "i-keepme" not in line
    ]
    assert len(minted) == 1 and minted[0].startswith("i-") and minted[0] != "i-keepme"


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


def test_sanitize_title_strips_label_preambles_and_markdown():
    """Real observed output: "**Message summary:** An incoming message" -
    a markdown-bolded label preamble, not a title."""
    title = wa_agent._sanitize_title("**Message summary:** An incoming message about billing")
    assert title == "An incoming message about billing"
    assert "*" not in title
    assert ":" not in title

    fenced = wa_agent._sanitize_title("```\nBooking the hall\n```")
    assert fenced == "Booking the hall"

    just_a_fence = wa_agent._sanitize_title("```", fallback_text="book the seminar hall")
    assert just_a_fence == "book the seminar hall"


def test_titles_and_summaries_keep_numbers_and_jids(vault: LocalDirClient):
    """ADR-0012 amends ADR-0009: this wiki is internal, so a phone
    number or jid in what members actually said is kept, not redacted."""
    assert wa_agent._sanitize_title("Call 98765 43210 about the venue") == "Call 98765 43210 about the"
    summary = wa_agent._sanitize_summary(
        "A contact (919244352208@s.whatsapp.net) asked about the venue booking."
    )
    assert "919244352208@s.whatsapp.net" in summary
    assert "[redacted]" not in summary


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


def test_sanitize_summary_caps_length():
    raw = "Sentence one. " * 200
    summary = wa_agent._sanitize_summary(raw)
    assert len(summary) <= wa_agent._SUMMARY_MAX_CHARS


def test_thread_update_contract_defaults_to_no_items():
    update = ThreadUpdate(title="A title", summary="A summary.")
    assert update.items == []


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


async def test_new_thread_dates_come_from_the_messages_not_the_clock(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    """Backfilled history must not look like it happened today -
    opened_at/last_message_at drive the stale check and the channel
    index's "last <date>"."""
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 2)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    channel = "891-dates@g.us"
    await _seed_channel(channel, title="Dates")
    async with get_session() as session:
        for i, ts in enumerate(("2026-09-18T10:02:00Z", "2026-09-19T11:30:00Z")):
            session.add(
                MessageBuffer(
                    message_id=f"dt-{i}",
                    channel=channel,
                    event_type="message.backfill",
                    payload=json.dumps(
                        message_event(f"dt-{i}", channel, f"old message {i}", sender=f"{i}@x", timestamp=ts)
                    ),
                )
            )
        await session.commit()

    result = await wa_agent.run_batch(channel, vault, _worker())
    assert result is not None

    page = parse_page((await vault.read(f"channels/dates/{result.threads_created[0]}.md")).content)
    assert page.frontmatter.opened_at == datetime(2026, 9, 18, 10, 2, tzinfo=UTC)
    assert page.frontmatter.last_message_at == datetime(2026, 9, 19, 11, 30, tzinfo=UTC)
    assert result.threads_created[0].startswith("20260918-")  # slug dated by the first message too


async def test_run_batch_never_ingests_the_logs_channel(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    """The logs channel is the bot talking to itself - no threads, no
    model calls - but its buffered rows are still drained so they don't
    pile up unprocessed forever."""
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 1)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    channel = "892-logs@g.us"
    async with get_session() as session:
        session.add(Channel(jid=channel, kind="logs", title="Logs"))
        await session.commit()
    ids = await _seed_messages(channel, ["⚠️ *ingest_tick* failed: boom"])

    try:
        worker = _worker()
        result = await wa_agent.run_batch(channel, vault, worker)
        assert result is not None
        assert result.threads_created == [] and result.threads_updated == []
        assert result.model_calls == 0
        assert worker.assignment_prompts == [] and worker.update_prompts == []
        assert await vault.list("channels/logs") == []

        async with get_session() as session:
            row = await session.scalar(select(MessageBuffer).where(MessageBuffer.message_id == ids[0]))
            chan = await session.scalar(select(Channel).where(Channel.jid == channel))
        assert row.processed is True  # drained, not left to accumulate
        assert chan.cursor == ids[0]
    finally:
        # `logs` is a singleton kind and the test DB is shared - leaving
        # this row behind breaks every later /setup logs test.
        async with get_session() as session:
            await session.execute(delete(Channel).where(Channel.jid == channel))
            await session.commit()
