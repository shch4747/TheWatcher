"""Reply-based auto-threading, same-sender concatenation, and the
per-batch structured assignment call (ADR-0012), plus the wiki-facing
rendering it feeds: sender names, sent times, and the channel index's
wikilinks."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agents.wa_agent import interface as wa_agent
from agents.wa_agent.assign import ItemOut
from shared.db import Channel, MembersRegistry, MessageBuffer, get_session
from shared.wiki.interface import LocalDirClient, parse_page

from tests.gowa_payloads import message_event
from tests.scripted_models import ScriptedStructuredWorker

CHANNEL_JID = "555-routing-test@g.us"


@pytest.fixture
def vault(tmp_path: Path) -> LocalDirClient:
    return LocalDirClient(tmp_path)


async def _seed_channel(jid: str = CHANNEL_JID, title: str = "Routing") -> None:
    async with get_session() as session:
        session.add(Channel(jid=jid, kind="project", title=title, initiative="Routing"))
        await session.commit()


async def _seed_raw_messages(jid: str, rows: list[dict]) -> None:
    """rows: [{"id", "text", "sender", "minutes_ago", "replied_to_id"?,
    "sender_name"?, "timestamp"?}]"""
    now = datetime.now(UTC)
    async with get_session() as session:
        for row in rows:
            payload = message_event(
                row["id"],
                jid,
                row["text"],
                sender=row["sender"],
                replied_to_id=row.get("replied_to_id"),
                sender_name=row.get("sender_name"),
                timestamp=row.get("timestamp"),
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
    worker = ScriptedStructuredWorker()
    result = await wa_agent.run_batch(channel, vault, worker)
    assert result is not None
    assert len(result.threads_created) == 1

    # one merged message -> one id in the assignment prompt, not two
    assert worker.assignment_prompts[0].count("- id=") == 1

    slug = result.threads_created[0]
    page = parse_page((await vault.read(f"channels/concat/{slug}.md")).content)
    assert "[src:: c0, c1]" in page.section("Timeline").body


async def test_run_batch_routes_reply_straight_to_target_thread_without_asking_the_model(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    """A reply to a message already on a thread page never reaches the
    model at all - with every message in the batch a reply, there is no
    assignment call to make."""
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
    worker = ScriptedStructuredWorker()
    result = await wa_agent.run_batch(channel, vault, worker)
    assert result is not None
    assert result.threads_updated == ["existing-thread"]
    assert worker.assignment_prompts == []  # no assignment call was needed
    assert result.model_calls == 1  # just the thread update

    page = parse_page((await vault.read("channels/reply/existing-thread.md")).content)
    timeline = page.section("Timeline").body
    assert "reply-1" in timeline
    assert "[reply_to:: orig-1]" in timeline  # the quote link survives on the page


async def test_reply_to_a_message_assigned_earlier_in_the_same_batch_is_routed_too(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 2)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    channel = "561-inbatch-reply@g.us"
    await _seed_channel(channel, title="InBatch")
    await _seed_raw_messages(
        channel,
        [
            {"id": "ib-1", "text": "can we book the hall", "sender": "a@x", "minutes_ago": 5},
            {"id": "ib-2", "text": "yes, Friday", "sender": "b@x", "minutes_ago": 4, "replied_to_id": "ib-1"},
        ],
    )
    worker = ScriptedStructuredWorker()
    result = await wa_agent.run_batch(channel, vault, worker)
    assert result is not None
    assert len(result.threads_created) == 1

    # only the non-reply was asked about; the reply rode along and shows
    # up as context, so the model still sees what it said
    prompt = worker.assignment_prompts[0]
    assert "## Replies within this batch" in prompt
    assert "(in reply to ib-1)" in prompt
    assert "yes, Friday" in prompt
    assert prompt[prompt.index("## Messages to assign"):].count("- id=") == 1

    slug = result.threads_created[0]
    timeline = parse_page((await vault.read(f"channels/inbatch/{slug}.md")).content).section("Timeline").body
    assert "[src:: ib-1]" in timeline and "[src:: ib-2]" in timeline


async def test_assignment_prompt_shows_existing_threads_with_title_summary_and_recent(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 1)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    channel = "562-context@g.us"
    await _seed_channel(channel, title="Ctx")
    await vault.write(
        "channels/ctx/20260101-hall.md",
        (
            "---\ntype: thread\nslug: 20260101-hall\nchannel: \"[[Ctx]]\"\n"
            "title: Booking the hall\nstate: stale\n---\n# Booking the hall\n"
            "## Summary\n<!-- watcher:managed -->\nDiscussing hall availability for the demo.\n"
            "<!-- /watcher -->\n## Items\n<!-- watcher:managed -->\n<!-- /watcher -->\n"
            "## Timeline\n<!-- watcher:append -->\n"
            "- 2026-01-01 10:00 — [[Aira]]: can we book it [src:: m1]\n<!-- /watcher -->\n## Notes\n"
        ),
        base_revision="",
    )
    await _seed_raw_messages(channel, [{"id": "ctx-1", "text": "any news?", "sender": "z@z"}])
    worker = ScriptedStructuredWorker(route={"ctx-1": "20260101-hall"})
    await wa_agent.run_batch(channel, vault, worker)

    prompt = worker.assignment_prompts[0]
    assert "### 20260101-hall [stale]" in prompt
    assert "Booking the hall" in prompt
    assert "Discussing hall availability" in prompt
    assert "can we book it" in prompt


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
    worker = ScriptedStructuredWorker(route={"rt-1": "old-title"})
    result = await wa_agent.run_batch(channel, vault, worker)
    assert result is not None
    assert result.threads_updated == ["old-title"]

    # the update call is given the current title, and its answer wins
    assert "Thread title: Old title" in worker.update_prompts[0]
    page = parse_page((await vault.read("channels/retitle/old-title.md")).content)
    assert page.frontmatter.title == "Booking the seminar hall"
    assert page.preamble.strip() == "# Booking the seminar hall"
    # last_message_at bumped - a revived/updated thread must not go
    # stale again on the very next lifecycle tick
    assert page.frontmatter.last_message_at is not None
    assert page.frontmatter.last_message_at.tzinfo is not None
    assert (datetime.now(UTC) - page.frontmatter.last_message_at) < timedelta(minutes=5)


async def test_run_batch_regenerates_channel_active_threads_section_as_wikilinks(
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
    result = await wa_agent.run_batch(channel, vault, ScriptedStructuredWorker())
    assert result is not None
    slug = result.threads_created[0]

    channel_page = parse_page((await vault.read("channels/idx.md")).content)
    active_body = channel_page.section("Active threads").body
    assert f"[[channels/idx/{slug}|Booking the seminar hall]]" in active_body
    assert "](http" not in active_body  # never a markdown link to a Lapis URL


async def test_run_batch_disambiguates_colliding_new_thread_slugs(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    """Two distinct new threads in one batch that the model happens to
    give the same title - a real production collision, since the slug is
    date+title. They must land on distinct paths, not overwrite."""
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 3)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    channel = "560-collide-test@g.us"
    await _seed_channel(channel, title="Collide")
    await _seed_raw_messages(
        channel,
        [
            {"id": "col-1", "text": "call about the venue", "sender": "a@x", "minutes_ago": 3},
            {"id": "col-2", "text": "just chatting, unrelated", "sender": "b@x", "minutes_ago": 2},
            {"id": "col-3", "text": "a totally different topic", "sender": "c@x", "minutes_ago": 1},
        ],
    )
    worker = ScriptedStructuredWorker(
        route={"col-1": "new-1", "col-2": "chatter", "col-3": "new-2"},
        new_thread_titles={"new-1": "Same title", "new-2": "Same title"},
        title="Same title",
    )
    result = await wa_agent.run_batch(channel, vault, worker)
    assert result is not None
    assert result.chatter_count == 1
    slugs = result.threads_created
    assert len(slugs) == 2
    assert len(set(slugs)) == 2  # distinct paths - no write collision
    assert slugs[1].endswith("-2")  # disambiguated, not silently overwritten

    for slug in slugs:
        page = parse_page((await vault.read(f"channels/collide/{slug}.md")).content)
        assert page.frontmatter.title == "Same title"


async def test_timeline_and_participants_use_member_name_then_display_name(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    """Name, not number: a linked sender is a wikilink, an unlinked one
    with a WhatsApp display name shows that name, and only a sender gowa
    gave no name for falls back to the jid."""
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 3)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    channel = "563-names@g.us"
    await _seed_channel(channel, title="Names")
    async with get_session() as session:
        session.add(MembersRegistry(wa_identity="919000000001@s.whatsapp.net", member_title="Aira"))
        await session.commit()
    await _seed_raw_messages(
        channel,
        [
            {
                "id": "nm-1", "text": "linked sender", "sender": "919000000001@s.whatsapp.net",
                "sender_name": "Aira J", "minutes_ago": 3, "timestamp": "2026-09-18T10:02:00Z",
            },
            {
                "id": "nm-2", "text": "unlinked but named", "sender": "919000000002@s.whatsapp.net",
                "sender_name": "Rohan", "minutes_ago": 2, "timestamp": "2026-09-18T10:03:00Z",
            },
            {
                "id": "nm-3", "text": "no name at all", "sender": "919000000003@s.whatsapp.net",
                "minutes_ago": 1, "timestamp": "2026-09-18T10:04:00Z",
            },
        ],
    )
    worker = ScriptedStructuredWorker(items=[ItemOut(kind="task", text="x", src_ids=["nm-1"])])
    result = await wa_agent.run_batch(channel, vault, worker)
    assert result is not None

    page = parse_page((await vault.read(f"channels/names/{result.threads_created[0]}.md")).content)
    timeline = page.section("Timeline").body
    assert "— [[Aira]]: linked sender" in timeline
    assert "— Rohan: unlinked but named" in timeline
    assert "— 919000000003@s.whatsapp.net: no name at all" in timeline
    # the sent time from gowa's payload, not our received_at
    assert "- 2026-09-18 10:02 — [[Aira]]" in timeline

    assert page.frontmatter.participants == [
        "919000000003@s.whatsapp.net",
        "Rohan",
        "[[Aira]]",
    ]

    # the model sees the resolved name plus the jid, so it can tell
    # same-named people apart without losing the number
    prompt = worker.assignment_prompts[0]
    assert "Aira (919000000001@s.whatsapp.net)" in prompt
    assert "Rohan (919000000002@s.whatsapp.net)" in prompt
    assert "2026-09-18 10:02" in prompt


async def test_run_batch_splits_into_several_assignment_calls_under_a_small_budget(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    """The whole path under a token budget too small for one call: the
    batch is split, and a thread minted by the first call still receives
    messages from the second (one thread, not two)."""
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 6)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)
    monkeypatch.setattr(settings, "ingest_max_messages_per_call", 2)

    channel = "564-chunked@g.us"
    await _seed_channel(channel, title="Chunked")
    await _seed_raw_messages(
        channel,
        [
            # alternating senders, so nothing folds into one logical message
            {
                "id": f"ch-{i}",
                "text": f"message about the hall {i}",
                "sender": f"{'a' if i % 2 else 'b'}@x",
                "minutes_ago": 6 - i,
            }
            for i in range(6)
        ],
    )
    worker = ScriptedStructuredWorker()  # everything -> new-1, chunk after chunk
    result = await wa_agent.run_batch(channel, vault, worker)
    assert result is not None

    assert len(worker.assignment_prompts) == 3  # 6 messages, 2 per call
    assert result.threads_created == [result.threads_created[0]]  # exactly one thread
    assert result.model_calls == 4  # 3 assignment calls + 1 thread update

    page = parse_page((await vault.read(f"channels/chunked/{result.threads_created[0]}.md")).content)
    timeline = page.section("Timeline").body
    for i in range(6):
        assert f"[src:: ch-{i}]" in timeline
