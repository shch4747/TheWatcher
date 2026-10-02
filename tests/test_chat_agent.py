"""Chat Agent: @mention answers from the last 5 messages; replies to the
bot do not trigger; a media-only mention gets a canned text reply."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from agents.wa_agent import interface as wa_agent
from agents.wa_agent.chat import CANNOT_SEE_MEDIA
from httpx import ASGITransport
from shared.db import MessageBuffer, OutboundLog, Proposal, get_session
from shared.gateway import interface as gateway
from shared.models.decision import FixtureDecisionModel
from shared.models.schemas import NoulResult, TextResult
from shared.models.text import TextModelClient
from shared.wiki.interface import LocalDirClient, parse_page
from sqlalchemy import select

from tests.fake_gowa.app import app as fake_gowa_app
from tests.gowa_payloads import inbound, message_event

CHANNEL_JID = "chat-agent-chan@g.us"
BOT_JID = "watcher-bot@s.whatsapp.net"


class EchoWorker(TextModelClient):
    def __init__(self, canned: str | Callable[[str], str] = "Hall is booked for Friday."):
        self.model_name = "echo-worker"
        self.canned = canned

    async def generate(self, prompt: str, system: str | None = None):  # type: ignore[override]
        text = self.canned(prompt) if callable(self.canned) else self.canned
        return TextResult(text=text, input_tokens=1, output_tokens=1)

    async def generate_with_tools(self, prompt: str, **kwargs):  # type: ignore[override]
        return await self.generate(prompt, system=kwargs.get("system"))


@pytest.fixture(autouse=True)
def _wire_fake_gowa(monkeypatch):
    fake_transport = ASGITransport(app=fake_gowa_app)
    fake_client = httpx.AsyncClient(transport=fake_transport, base_url="http://fake-gowa")
    monkeypatch.setattr(gateway._client, "_client", fake_client)
    monkeypatch.setattr(gateway._client, "base_url", "http://fake-gowa")
    monkeypatch.setattr("shared.config.settings.gowa_device_id", BOT_JID)
    fake_gowa_app.state.sent_messages = []


@pytest.fixture
def vault(tmp_path: Path) -> LocalDirClient:
    return LocalDirClient(tmp_path)


def _message(text: str, message_id: str, **kwargs):
    return inbound(message_id, CHANNEL_JID, text, **kwargs)


async def _buffer(
    channel: str,
    message_id: str,
    text: str,
    sender: str = "a@x",
    replied_to_id: str | None = None,
) -> None:
    async with get_session() as session:
        session.add(
            MessageBuffer(
                message_id=message_id,
                channel=channel,
                event_type="message",
                payload=json.dumps(
                    message_event(
                        message_id, channel, text, sender=sender, replied_to_id=replied_to_id
                    )
                ),
            )
        )
        await session.commit()


async def test_non_addressed_message_is_ignored(vault: LocalDirClient):
    reply = await wa_agent.handle_chat_message(_message("just chatting", "m1"), EchoWorker(), vault)
    assert reply is None
    assert fake_gowa_app.state.sent_messages == []


async def test_reply_to_bot_does_not_trigger(vault: LocalDirClient):
    async with get_session() as session:
        session.add(OutboundLog(channel=CHANNEL_JID, text="watching", message_id="bot-msg-1"))
        await session.commit()

    message = inbound("q2", CHANNEL_JID, "and when is it due?", replied_to_id="bot-msg-1")
    reply = await wa_agent.handle_chat_message(message, EchoWorker(), vault)
    assert reply is None
    assert fake_gowa_app.state.sent_messages == []


async def test_mention_answers_and_quotes_the_trigger(vault: LocalDirClient):
    message = _message("@watcher who owns the hall booking?", "q1")
    reply = await wa_agent.handle_chat_message(message, EchoWorker(), vault)

    assert reply == "Hall is booked for Friday."
    sent = fake_gowa_app.state.sent_messages[-1]
    assert sent["message"] == "Hall is booked for Friday."
    assert sent.get("reply_message_id") == "q1"


async def test_answer_uses_only_the_last_five_messages(vault: LocalDirClient):
    for i in range(8):
        await _buffer(CHANNEL_JID, f"win-{i}", f"OLD-{i} hall chatter")

    def from_prompt(prompt: str) -> str:
        return "SAW_OLD" if "OLD-0" in prompt or "OLD-2" in prompt else "FROM_RECENT"

    message = _message("@watcher what's the plan?", "q-recent")
    reply = await wa_agent.handle_chat_message(message, EchoWorker(from_prompt), vault)
    assert reply == "FROM_RECENT"


async def test_mention_that_replies_pulls_the_quoted_message(vault: LocalDirClient):
    await _buffer(CHANNEL_JID, "anchor-sat", "ANCHOR-the hall is Saturday")
    for i in range(8):
        await _buffer(CHANNEL_JID, f"gap-{i}", f"OLD-{i} hall chatter")

    def from_prompt(prompt: str) -> str:
        if "ANCHOR-the hall is Saturday" not in prompt:
            return "MISSED_QUOTE"
        if "OLD-0" in prompt or "OLD-2" in prompt:
            return "SAW_OLD"
        return "GOT_QUOTE"

    message = inbound(
        "q-reply", CHANNEL_JID, "@watcher is that still true?", replied_to_id="anchor-sat"
    )
    reply = await wa_agent.handle_chat_message(message, EchoWorker(from_prompt), vault)
    assert reply == "GOT_QUOTE"


async def test_quoted_messages_of_the_last_five_are_included_one_hop(vault: LocalDirClient):
    await _buffer(CHANNEL_JID, "deep-1", "DEEP-never-in-window")
    await _buffer(CHANNEL_JID, "mid-1", "MID-quoted-by-recent", replied_to_id="deep-1")
    for i in range(8):
        await _buffer(CHANNEL_JID, f"hop-{i}", f"FILL-{i}")
    await _buffer(CHANNEL_JID, "recent-reply", "asking about mid", replied_to_id="mid-1")

    def from_prompt(prompt: str) -> str:
        if "MID-quoted-by-recent" not in prompt:
            return "MISSED_MID"
        if "DEEP-never-in-window" in prompt:
            return "SAW_DEEP"
        return "ONE_HOP"

    message = _message("@watcher summarise that", "q-hop")
    reply = await wa_agent.handle_chat_message(message, EchoWorker(from_prompt), vault)
    assert reply == "ONE_HOP"


async def test_mention_with_image_and_no_text_says_cannot_see(vault: LocalDirClient):
    message = inbound("img1", CHANNEL_JID, "", media_kind="image", mentions=[BOT_JID])
    reply = await wa_agent.handle_chat_message(message, EchoWorker("should not call me"), vault)
    assert reply == CANNOT_SEE_MEDIA
    sent = fake_gowa_app.state.sent_messages[-1]
    assert sent["message"] == CANNOT_SEE_MEDIA
    assert sent.get("reply_message_id") == "img1"


async def test_mention_sticker_with_no_text_says_cannot_see(vault: LocalDirClient):
    message = inbound("stk1", CHANNEL_JID, "", media_kind="sticker", mentions=[BOT_JID])
    reply = await wa_agent.handle_chat_message(message, EchoWorker("should not call me"), vault)
    assert reply == CANNOT_SEE_MEDIA
    sent = fake_gowa_app.state.sent_messages[-1]
    assert sent["message"] == CANNOT_SEE_MEDIA


async def test_mention_image_with_caption_still_answers(vault: LocalDirClient):
    message = inbound("img2", CHANNEL_JID, "@watcher what is this?", media_kind="image")
    reply = await wa_agent.handle_chat_message(message, EchoWorker(), vault)
    assert reply == "Hall is booked for Friday."


async def test_own_message_is_ignored(vault: LocalDirClient):
    message = inbound("me1", CHANNEL_JID, "@watcher hello", from_me=True)
    assert await wa_agent.handle_chat_message(message, EchoWorker(), vault) is None


async def test_write_verbs_in_a_mention_are_just_answered(vault: LocalDirClient):
    message = _message("@watcher note the demo moved to Friday", "w1")
    reply = await wa_agent.handle_chat_message(message, EchoWorker(), vault)
    assert reply == "Hall is booked for Friday."
    async with get_session() as session:
        proposal = await session.scalar(select(Proposal))
    assert proposal is None


async def test_confirming_wiki_write_proposal_appends_to_notes(vault: LocalDirClient):
    async with get_session() as session:
        from shared.db import Channel

        session.add(Channel(jid="confirm-write-chan@g.us", kind="project", title="ConfirmWrite"))
        await session.commit()
    path = "channels/confirmwrite/some-thread.md"
    await vault.create(
        path,
        (
            "---\ntype: thread\nslug: some-thread\nchannel: \"[[ConfirmWrite (WhatsApp)]]\"\n"
            "title: Some thread\nstate: active\n---\n# Some thread\n"
            "## Summary\n<!-- watcher:managed -->\nplaceholder.\n<!-- /watcher -->\n"
            "## Notes\n"
        ),
    )

    await wa_agent.propose_wiki_write(
        "confirm-write-chan@g.us", path, "Notes", "- the demo moved to Friday"
    )
    async with get_session() as session:
        proposal = await session.scalar(
            select(Proposal).where(Proposal.channel == "confirm-write-chan@g.us")
        )

    reply = await wa_agent.confirm_proposal(proposal.id, "admin@x", vault=vault)
    assert "Added to" in reply

    updated = parse_page((await vault.read(path)).content)
    assert "the demo moved to Friday" in updated.section("Notes").body


async def test_interpret_text_approval_uses_decision_model_noul():
    decision = FixtureDecisionModel(
        nouls={
            'Is this an approval? "yes go ahead"': NoulResult(answer=True, confidence=0.9),
            'Is this an approval? "no wait"': NoulResult(answer=False, confidence=0.9),
        }
    )
    assert await wa_agent.interpret_text_approval("yes go ahead", decision) is True
    assert await wa_agent.interpret_text_approval("no wait", decision) is False


def test_is_bot_mention_and_is_write_request():
    assert wa_agent.is_bot_mention("@watcher hello") is True
    assert wa_agent.is_bot_mention("hello there") is False
    assert wa_agent.is_write_request("note that the demo moved") is True
    assert wa_agent.is_write_request("who owns this?") is False


THREAD_PAGE = (
    '---\ntype: thread\nslug: hall-booking\nchannel: "[[ChatAgentProj (WhatsApp)]]"\n'
    "title: Hall booking\nstate: active\n---\n# Hall booking\n"
    "## Summary\n<!-- watcher:managed -->\nAira is booking the seminar hall for the demo.\n"
    "<!-- /watcher -->\n"
    "## Items\n<!-- watcher:managed -->\n"
    "- [ ] Book the hall [owner:: [[Aira]]] [src:: orig-msg-1] ^i-0001\n<!-- /watcher -->\n"
    "## Timeline\n<!-- watcher:append -->\n"
    "- 2026-09-18 10:00 — [[Devansh]] asks about the hall [src:: orig-msg-1]\n"
    "<!-- /watcher -->\n## Notes\n"
)


async def test_wiki_tools_list_read_and_search(vault: LocalDirClient):
    from agents.wa_agent.wiki_tools import list_threads, read_page, search_wiki
    from shared.db import Channel
    from shared.gateway.interface import ChannelInfo

    async with get_session() as session:
        session.add(
            Channel(jid=CHANNEL_JID, kind="project", title="ChatAgentProj", initiative="ChatAgentProj")
        )
        await session.commit()
    await vault.create("channels/chatagentproj/hall-booking.md", THREAD_PAGE)
    channel = ChannelInfo(
        jid=CHANNEL_JID, title="ChatAgentProj", kind="project", initiative="ChatAgentProj"
    )

    listed = await list_threads(vault, channel)
    assert "hall-booking.md" in listed
    assert "Hall booking" in listed

    page = await read_page(vault, "channels/chatagentproj/hall-booking.md")
    assert "Aira is booking the seminar hall" in page
    assert await read_page(vault, "channels/missing.md") == "No page at channels/missing.md."

    found = await search_wiki(vault, "seminar hall", prefix="channels/chatagentproj")
    assert "hall-booking.md" in found
    assert "Aira" in found
    empty = await search_wiki(vault, "zzzz-not-in-vault")
    assert "No matches" in empty
