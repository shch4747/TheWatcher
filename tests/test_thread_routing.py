"""Reply-based auto-threading, same-sender concatenation, and the
richer per-thread Choice context (user-reported: "channel management
and thread decision... completely broken" - bare slugs with no
title/summary/recent-context, no reply short-circuit, no concatenation,
no live channel-page updates, titles not word-capped or regenerated
for existing threads)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agents.wa_agent import interface as wa_agent
from shared.db import Channel, MessageBuffer, get_session
from shared.models.decision import FixtureDecisionModel
from shared.models.schemas import ChoiceResult
from shared.models.text import TextModelClient
from shared.wiki.interface import LocalDirClient, parse_page

from tests.gowa_payloads import message_event

CHANNEL_JID = "555-routing-test@g.us"


class ScriptedWorker(TextModelClient):
    def __init__(self):
        self.model_name = "scripted-worker"

    async def generate(self, prompt: str, system: str | None = None):  # type: ignore[override]
        from shared.models.schemas import TextResult

        if system and "Summary" in system:
            text = "A test thread."
        elif system and "Items" in system:
            text = ""
        elif system and "title" in system:
            text = "Booking the seminar hall"
        else:
            text = prompt.strip().split("\n")[-1]
        return TextResult(text=text, input_tokens=1, output_tokens=1)


@pytest.fixture
def vault(tmp_path: Path) -> LocalDirClient:
    return LocalDirClient(tmp_path)


async def _seed_channel(jid: str = CHANNEL_JID, title: str = "Routing") -> None:
    async with get_session() as session:
        session.add(Channel(jid=jid, kind="project", title=title, initiative="Routing"))
        await session.commit()


async def _seed_raw_messages(jid: str, rows: list[dict]) -> None:
    """rows: [{"id", "text", "sender", "minutes_ago", "replied_to_id"?}]"""
    now = datetime.now(UTC)
    async with get_session() as session:
        for row in rows:
            payload = message_event(
                row["id"], jid, row["text"], sender=row["sender"], replied_to_id=row.get("replied_to_id")
            )
            buf = MessageBuffer(
                message_id=row["id"], channel=jid, event_type="message", payload=json.dumps(payload)
            )
            buf.received_at = now - timedelta(minutes=row.get("minutes_ago", 0))
            session.add(buf)
        await session.commit()


def test_concatenate_same_sender_within_window_merges_plain_messages():
    now = datetime.now(UTC)
    msgs = [
        wa_agent.BufferedMessage(1, "m0", CHANNEL_JID, now, "hi", "a@x"),
        wa_agent.BufferedMessage(2, "m1", CHANNEL_JID, now + timedelta(minutes=1), "there", "a@x"),
        wa_agent.BufferedMessage(3, "m2", CHANNEL_JID, now + timedelta(minutes=5), "later one", "a@x"),
    ]
    merged = wa_agent._concatenate_same_sender(msgs)
    assert len(merged) == 2  # m0+m1 merge (1 min apart), m2 is its own (4 min after m1)
    assert merged[0].text == "hi\nthere"
    assert merged[0].src_ids == ["m0", "m1"]
    assert merged[1].src_ids == ["m2"]


def test_concatenate_does_not_merge_across_senders_or_replies():
    now = datetime.now(UTC)
    msgs = [
        wa_agent.BufferedMessage(1, "m0", CHANNEL_JID, now, "hi", "a@x"),
        wa_agent.BufferedMessage(2, "m1", CHANNEL_JID, now + timedelta(seconds=30), "hi back", "b@x"),
        wa_agent.BufferedMessage(
            3, "m2", CHANNEL_JID, now + timedelta(minutes=1), "replying", "a@x", replied_to_id="m1"
        ),
    ]
    merged = wa_agent._concatenate_same_sender(msgs)
    assert len(merged) == 3  # different sender, then a reply - neither merges
    assert [m.src_ids for m in merged] == [["m0"], ["m1"], ["m2"]]


async def test_run_batch_concatenates_quick_same_sender_messages(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 2)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    channel = "556-concat-test@g.us"
    await _seed_channel(channel, title="Concat")
    await _seed_raw_messages(
        channel,
        [
            {"id": "c0", "text": "book the hall", "sender": "x@y", "minutes_ago": 2},
            {"id": "c1", "text": "for the demo", "sender": "x@y", "minutes_ago": 1},
        ],
    )
    question = "Which thread does this message belong to?\n\nMessage: book the hall\nfor the demo"
    decision = FixtureDecisionModel(
        choices={question: ChoiceResult(option="new-thread", probabilities={"new-thread": 0.95})}
    )
    result = await wa_agent.run_batch(channel, vault, decision, ScriptedWorker())
    assert result is not None
    assert len(result.threads_created) == 1  # one merged message -> one Choice call -> one thread

    slug = result.threads_created[0]
    page = parse_page((await vault.read(f"channels/concat/{slug}.md")).content)
    assert "[src:: c0, c1]" in page.section("Timeline").body


async def test_run_batch_routes_reply_straight_to_target_thread_no_choice_call(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    """A FixtureDecisionModel with NO recorded choice for the reply's
    question - if the reply short-circuit didn't work, this would raise
    KeyError (FixtureDecisionModel.choice never guesses)."""
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 1)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    channel = "557-reply-test@g.us"
    await _seed_channel(channel, title="Reply")
    await vault.write(
        "channels/reply/existing-thread.md",
        (
            "---\ntype: thread\nslug: existing-thread\n"
            'channel: "[[Reply]]"\ntitle: Existing thread\nstate: active\n'
            "opened_at: 2026-01-01T00:00:00Z\nlast_message_at: 2026-01-01T00:00:00Z\n"
            'participants: []\nmessage_ids: ["orig-1"]\n---\n# Existing thread\n'
            "## Summary\n<!-- watcher:managed -->\nsomething\n<!-- /watcher -->\n"
            "## Items\n<!-- watcher:managed -->\n<!-- /watcher -->\n"
            "## Timeline\n<!-- watcher:append -->\n"
            "- 2026-01-01 00:00 — x orig message [src:: orig-1]\n<!-- /watcher -->\n"
            "## Notes\n"
        ),
        base_revision="",
    )
    await _seed_raw_messages(
        channel,
        [{"id": "reply-1", "text": "yes agreed", "sender": "y@z", "replied_to_id": "orig-1"}],
    )
    decision = FixtureDecisionModel()  # no fixtures recorded at all
    result = await wa_agent.run_batch(channel, vault, decision, ScriptedWorker())
    assert result is not None
    assert result.threads_updated == ["existing-thread"]

    page = parse_page((await vault.read("channels/reply/existing-thread.md")).content)
    assert "reply-1" in page.section("Timeline").body


async def test_choice_options_carry_title_summary_and_recent_context():
    """The core reported bug: options used to be bare slugs. Now every
    active-thread option's description embeds title + summary + a
    recent-messages excerpt."""
    thread = wa_agent.ThreadInfo(
        slug="20260101-hall",
        path="channels/x/20260101-hall.md",
        title="Booking the hall",
        summary="Discussing hall availability for the demo.",
        state="active",
        recent_context="- 2026-01-01 10:00 — x can we book it [src:: m1]",
    )
    context = wa_agent._thread_context(thread)
    assert "Booking the hall" in context
    assert "Discussing hall availability" in context
    assert "can we book it" in context


def test_sanitize_title_caps_at_five_words():
    long_title = "This is a very long title with way too many words"
    title = wa_agent._sanitize_title(long_title)
    assert len(title.split(" ")) <= wa_agent._TITLE_MAX_WORDS


async def test_run_batch_retitles_existing_thread_with_old_title_as_context(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 1)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    channel = "558-retitle-test@g.us"
    await _seed_channel(channel, title="Retitle")
    await vault.write(
        "channels/retitle/old-title.md",
        (
            "---\ntype: thread\nslug: old-title\n"
            'channel: "[[Retitle]]"\ntitle: Old title\nstate: active\n'
            "opened_at: 2026-01-01T00:00:00Z\nlast_message_at: 2026-01-01T00:00:00Z\n"
            'participants: []\nmessage_ids: []\n---\n# Old title\n'
            "## Summary\n<!-- watcher:managed -->\nsomething\n<!-- /watcher -->\n"
            "## Items\n<!-- watcher:managed -->\n<!-- /watcher -->\n"
            "## Timeline\n<!-- watcher:append -->\n<!-- /watcher -->\n"
            "## Notes\n"
        ),
        base_revision="",
    )
    await _seed_raw_messages(channel, [{"id": "rt-1", "text": "new info", "sender": "z@z"}])
    question = "Which thread does this message belong to?\n\nMessage: new info"
    decision = FixtureDecisionModel(
        choices={question: ChoiceResult(option="old-title", probabilities={"old-title": 0.9})}
    )
    result = await wa_agent.run_batch(channel, vault, decision, ScriptedWorker())
    assert result is not None
    assert result.threads_updated == ["old-title"]

    page = parse_page((await vault.read("channels/retitle/old-title.md")).content)
    assert page.frontmatter.title == "Booking the seminar hall"  # ScriptedWorker's title output
    assert page.preamble.strip() == "# Booking the seminar hall"
    # last_message_at bumped - a revived/updated thread must not go
    # stale again on the very next lifecycle tick
    assert page.frontmatter.last_message_at is not None
    assert page.frontmatter.last_message_at.tzinfo is not None
    assert (datetime.now(UTC) - page.frontmatter.last_message_at) < timedelta(minutes=5)


async def test_run_batch_regenerates_channel_active_threads_section(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    from shared.config import settings
    from shared.wiki.interface import render_new_channel_page

    monkeypatch.setattr(settings, "batch_n", 1)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    channel = "559-index-test@g.us"
    await _seed_channel(channel, title="Idx")
    await vault.write("channels/idx.md", render_new_channel_page("Idx", "project"), base_revision="")
    await _seed_raw_messages(channel, [{"id": "idx-1", "text": "book the hall", "sender": "x@y"}])
    question = "Which thread does this message belong to?\n\nMessage: book the hall"
    decision = FixtureDecisionModel(
        choices={question: ChoiceResult(option="new-thread", probabilities={"new-thread": 0.95})}
    )
    result = await wa_agent.run_batch(channel, vault, decision, ScriptedWorker())
    assert result is not None
    slug = result.threads_created[0]

    channel_page = parse_page((await vault.read("channels/idx.md")).content)
    active_body = channel_page.section("Active threads").body
    assert slug in active_body or "Booking the seminar hall" in active_body


class UntitledWorker(TextModelClient):
    """Every title request comes back PII-laden (rejected by
    contains_pii, falling back to "Untitled thread") - reproduces a
    real production collision: two distinct new threads created in the
    same batch, on the same day, both landing on the same slug."""

    def __init__(self):
        self.model_name = "untitled-worker"

    async def generate(self, prompt: str, system: str | None = None):  # type: ignore[override]
        from shared.models.schemas import TextResult

        if system and "Summary" in system:
            text = "A test thread."
        elif system and "Items" in system:
            text = ""
        elif system and "title" in system:
            text = "919244352208@s.whatsapp.net"  # PII - always rejected
        else:
            text = ""
        return TextResult(text=text, input_tokens=1, output_tokens=1)


async def test_run_batch_disambiguates_colliding_new_thread_slugs(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 5)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    monkeypatch.setattr(settings, "batch_n", 3)
    channel = "560-collide-test@g.us"
    await _seed_channel(channel, title="Collide")
    await _seed_raw_messages(
        channel,
        [
            {"id": "col-1", "text": "call about the venue", "sender": "a@x"},
            {"id": "col-2", "text": "just chatting, unrelated", "sender": "b@x"},
            {"id": "col-3", "text": "a totally different topic", "sender": "c@x"},
        ],
    )
    decision = FixtureDecisionModel(
        choices={
            "Which thread does this message belong to?\n\nMessage: call about the venue": ChoiceResult(
                option="new-thread", probabilities={"new-thread": 0.95}
            ),
            "Which thread does this message belong to?\n\nMessage: just chatting, unrelated": ChoiceResult(
                option="chatter", probabilities={"chatter": 0.95}
            ),
            "Which thread does this message belong to?\n\nMessage: a totally different topic": ChoiceResult(
                option="new-thread", probabilities={"new-thread": 0.95}
            ),
        }
    )
    result = await wa_agent.run_batch(channel, vault, decision, UntitledWorker())
    assert result is not None
    assert len(result.threads_created) == 2
    slugs = result.threads_created
    assert len(set(slugs)) == 2  # distinct paths - no write collision
    assert slugs[1].endswith("-2")  # disambiguated, not silently overwritten

    for slug in slugs:
        page = parse_page((await vault.read(f"channels/collide/{slug}.md")).content)
        assert page.frontmatter.title == "Untitled thread"
