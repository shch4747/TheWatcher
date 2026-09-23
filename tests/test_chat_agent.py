"""Phase 5 Chat Agent acceptance criteria (docs/Plan - Watcher v1.md):
"@bot who owns X" answers from the thread quoting the trigger; "@bot
note X" produces a Proposal, not a direct write; a text reply is
interpreted as approval via Decision Model Noul."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from agents.wa_agent import interface as wa_agent
from httpx import ASGITransport
from shared.db import Channel, OutboundLog, Proposal, get_session
from shared.gateway import interface as gateway
from shared.models.decision import FixtureDecisionModel
from shared.models.schemas import NoulResult
from shared.models.text import TextModelClient
from shared.wiki.interface import LocalDirClient, parse_page
from sqlalchemy import select

from tests.fake_gowa.app import app as fake_gowa_app
from tests.gowa_payloads import inbound

CHANNEL_JID = "chat-agent-chan@g.us"


class EchoWorker(TextModelClient):
    def __init__(self, canned: str = "Aira owns the hall booking."):
        self.model_name = "echo-worker"
        self.canned = canned

    async def generate(self, prompt: str, system: str | None = None):  # type: ignore[override]
        from shared.models.schemas import TextResult

        return TextResult(text=self.canned, input_tokens=1, output_tokens=1)


@pytest.fixture
def vault(tmp_path: Path) -> LocalDirClient:
    return LocalDirClient(tmp_path)


@pytest.fixture(autouse=True)
def _wire_fake_gowa(monkeypatch):
    fake_transport = ASGITransport(app=fake_gowa_app)
    fake_client = httpx.AsyncClient(transport=fake_transport, base_url="http://fake-gowa")
    monkeypatch.setattr(gateway._client, "_client", fake_client)
    monkeypatch.setattr(gateway._client, "base_url", "http://fake-gowa")
    fake_gowa_app.state.sent_messages = []


async def _seed_channel(jid: str = CHANNEL_JID, title: str = "ChatAgentProj") -> str:
    async with get_session() as session:
        session.add(Channel(jid=jid, kind="project", title=title, initiative=title))
        await session.commit()
    from shared.wiki.interface import slugify

    return slugify(title)


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


def _message(text: str, message_id: str, replied_to: str | None = None):
    return inbound(message_id, CHANNEL_JID, text, replied_to_id=replied_to)


async def test_non_addressed_message_is_ignored(vault: LocalDirClient):
    reply = await wa_agent.handle_chat_message(
        _message("just chatting", "m1"), vault, FixtureDecisionModel(), EchoWorker()
    )
    assert reply is None


async def test_mention_with_question_answers_from_thread_and_quotes(vault: LocalDirClient):
    channel_dir = await _seed_channel()
    await vault.create(f"channels/{channel_dir}/hall-booking.md", THREAD_PAGE)

    message = _message("@watcher who owns the hall booking?", "q1")
    reply = await wa_agent.handle_chat_message(message, vault, FixtureDecisionModel(), EchoWorker())

    assert reply == "Aira owns the hall booking."
    sent = fake_gowa_app.state.sent_messages[-1]
    assert sent["message"] == "Aira owns the hall booking."
    assert sent.get("reply_message_id") == "q1"


async def test_reply_to_bot_message_also_triggers(vault: LocalDirClient):
    channel_dir = await _seed_channel("reply-trigger-chan@g.us", "ReplyTrigger")
    await vault.create(f"channels/{channel_dir}/hall-booking.md", THREAD_PAGE)

    async with get_session() as session:
        session.add(OutboundLog(channel="reply-trigger-chan@g.us", text="watching", message_id="bot-msg-1"))
        await session.commit()

    message = inbound("q2", "reply-trigger-chan@g.us", "and when is it due?", replied_to_id="bot-msg-1")
    reply = await wa_agent.handle_chat_message(message, vault, FixtureDecisionModel(), EchoWorker())
    assert reply is not None


async def test_write_request_produces_a_proposal_not_a_direct_write(vault: LocalDirClient):
    channel_dir = await _seed_channel("write-req-chan@g.us", "WriteReq")
    thread_page = THREAD_PAGE.replace('slug: hall-booking', 'slug: demo-date').replace(
        "ChatAgentProj", "WriteReq"
    )
    await vault.create(f"channels/{channel_dir}/demo-date.md", thread_page)

    message = inbound("w1", "write-req-chan@g.us", "@watcher note the demo moved to Friday")

    before = (await vault.read(f"channels/{channel_dir}/demo-date.md")).content
    reply = await wa_agent.handle_chat_message(message, vault, FixtureDecisionModel(), EchoWorker())
    after = (await vault.read(f"channels/{channel_dir}/demo-date.md")).content

    assert "confirm" in reply.lower() or "👍" in reply
    assert before == after  # nothing written to the wiki yet - it's a Proposal

    async with get_session() as session:
        proposal = await session.scalar(
            select(Proposal).where(Proposal.channel == "write-req-chan@g.us")
        )
    assert proposal is not None
    assert proposal.status == "pending"
    data = json.loads(proposal.payload)
    assert "demo moved to Friday" in data["line"]


async def test_confirming_wiki_write_proposal_appends_to_notes(vault: LocalDirClient):
    channel_dir = await _seed_channel("confirm-write-chan@g.us", "ConfirmWrite")
    thread_page = THREAD_PAGE.replace('slug: hall-booking', 'slug: some-thread').replace(
        "ChatAgentProj", "ConfirmWrite"
    )
    path = f"channels/{channel_dir}/some-thread.md"
    await vault.create(path, thread_page)

    await gateway.propose_wiki_write("confirm-write-chan@g.us", path, "Notes", "- the demo moved to Friday")
    async with get_session() as session:
        proposal = await session.scalar(
            select(Proposal).where(Proposal.channel == "confirm-write-chan@g.us")
        )

    reply = await gateway.confirm_proposal(proposal.id, "admin@x", vault=vault)
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
