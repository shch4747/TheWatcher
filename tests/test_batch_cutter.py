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
