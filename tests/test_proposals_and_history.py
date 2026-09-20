"""Phase 2 acceptance criteria (docs/Plan - Watcher v1.md): a posted
Proposal executes on a 👍 from a Bot Admin within 24h; expired proposals
are dropped with a notice; request_history backfills a channel."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport
from shared.db import BotAdmin, Channel, MembersRegistry, MessageBuffer, Proposal, get_session
from shared.gateway import interface as gateway
from shared.gateway.app import app as gateway_app
from sqlalchemy import select

from tests.fake_gowa.app import app as fake_gowa_app
from tests.gowa_payloads import reaction_event

ADMIN = "919999911111@s.whatsapp.net"


def _sign(body: bytes) -> str:
    import hashlib
    import hmac

    from shared.config import settings

    return hmac.new(settings.gowa_webhook_secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
async def _seed_admin():
    async with get_session() as session:
        if await session.get(BotAdmin, ADMIN) is None:
            session.add(BotAdmin(wa_identity=ADMIN))
            await session.commit()


@pytest.fixture(autouse=True)
def _wire_fake_gowa(monkeypatch):
    fake_transport = ASGITransport(app=fake_gowa_app)
    fake_client = httpx.AsyncClient(transport=fake_transport, base_url="http://fake-gowa")
    monkeypatch.setattr(gateway._client, "_client", fake_client)
    monkeypatch.setattr(gateway._client, "base_url", "http://fake-gowa")
    fake_gowa_app.state.sent_messages = []
    fake_gowa_app.state.reactions = []
    fake_gowa_app.state.chat_history = {}


async def _make_pending_proposal(channel: str, message_id: str) -> int:
    async with get_session() as session:
        proposal = Proposal(
            channel=channel,
            kind="link_member",
            payload='{"wa_identity": "someone@x", "member_title": "Aira", "cms_member_id": "cms-1"}',
            message_id=message_id,
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        session.add(proposal)
        await session.commit()
        return proposal.id


async def test_thumbs_up_from_admin_confirms_the_proposal():
    proposal_id = await _make_pending_proposal("chan-1", "wamid.PROP1")
    reply = await gateway.handle_reaction(ADMIN, "wamid.PROP1", "\U0001f44d")
    assert reply is not None
    assert "Linked" in reply

    async with get_session() as session:
        proposal = await session.get(Proposal, proposal_id)
        registry = await session.get(MembersRegistry, "someone@x")
    assert proposal.status == "confirmed"
    assert registry is not None


async def test_thumbs_up_from_non_admin_is_ignored():
    await _make_pending_proposal("chan-2", "wamid.PROP2")
    reply = await gateway.handle_reaction("random@nobody", "wamid.PROP2", "\U0001f44d")
    assert reply is None

    async with get_session() as session:
        proposal = await session.scalar(select(Proposal).where(Proposal.message_id == "wamid.PROP2"))
    assert proposal.status == "pending"


async def test_other_emoji_reaction_does_nothing():
    await _make_pending_proposal("chan-3", "wamid.PROP3")
    reply = await gateway.handle_reaction(ADMIN, "wamid.PROP3", "\U0001f602")
    assert reply is None


async def test_expire_stale_proposals_marks_expired_and_notifies():
    async with get_session() as session:
        proposal = Proposal(
            channel="notify-chan@g.us",
            kind="link_member",
            payload="{}",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.add(proposal)
        await session.commit()
        proposal_id = proposal.id

    count = await gateway.expire_stale_proposals()
    assert count >= 1

    async with get_session() as session:
        row = await session.get(Proposal, proposal_id)
    assert row.status == "expired"
    assert any("expired" in m.get("message", "") for m in fake_gowa_app.state.sent_messages)


async def test_get_message_and_get_messages_and_get_context():
    channel = "ctx-chan@g.us"
    async with get_session() as session:
        session.add(Channel(jid=channel, kind="project"))
        for i in range(5):
            session.add(
                MessageBuffer(
                    message_id=f"m{i}",
                    channel=channel,
                    event_type="message",
                    payload=f'{{"id":"m{i}","n":{i}}}',
                )
            )
        await session.commit()

    assert (await gateway.get_message("m2"))["n"] == 2
    assert await gateway.get_message("nope") is None

    msgs = await gateway.get_messages(channel, limit=10)
    assert [m["n"] for m in msgs] == [0, 1, 2, 3, 4]

    since = await gateway.get_messages(channel, since_id="m1", limit=10)
    assert [m["n"] for m in since] == [2, 3, 4]

    ctx = await gateway.get_context("m2", before=1, after=1)
    assert [m["n"] for m in ctx] == [1, 2, 3]


async def test_request_history_backfills_and_flags_source():
    channel = "backfill-chan@g.us"
    fake_gowa_app.state.chat_history[channel] = [
        {"id": "hist-1", "chat_jid": channel, "sender_jid": "x@s.whatsapp.net", "content": "old message 1"},
        {"id": "hist-2", "chat_jid": channel, "sender_jid": "x@s.whatsapp.net", "content": "old message 2"},
    ]

    added = await gateway.request_history(channel, count=100)
    assert added == 2

    async with get_session() as session:
        rows = await session.scalars(select(MessageBuffer).where(MessageBuffer.channel == channel))
    rows = list(rows)
    assert len(rows) == 2
    assert all(r.event_type == "message.backfill" for r in rows)
    assert all("old message" in r.payload for r in rows)

    # calling it again doesn't duplicate what's already buffered
    added_again = await gateway.request_history(channel, count=100)
    assert added_again == 0


async def test_react_sends_via_gowa():
    await gateway.react("wamid.TARGET", "some-chan@g.us", "✅")
    assert fake_gowa_app.state.reactions[-1]["message_id"] == "wamid.TARGET"
    assert fake_gowa_app.state.reactions[-1]["emoji"] == "✅"


async def test_webhook_reaction_event_confirms_proposal_end_to_end():
    import json

    channel = "webhook-reaction-chan@g.us"
    async with get_session() as session:
        session.add(Channel(jid=channel, kind="project"))
        await session.commit()
    proposal_id = await _make_pending_proposal(channel, "wamid.PROPWEBHOOK")

    payload = reaction_event("wamid.REACTEVENT1", channel, ADMIN, "\U0001f44d", "wamid.PROPWEBHOOK")
    body = json.dumps(payload).encode()
    transport = ASGITransport(app=gateway_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/webhook/gowa", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert resp.status_code == 200

    async with get_session() as session:
        proposal = await session.get(Proposal, proposal_id)
    assert proposal.status == "confirmed"
