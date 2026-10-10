"""End-to-end webhook reaction tests for red-cross undo."""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from agents.wa_agent import interface as wa_agent
from httpx import ASGITransport
from shared.config import settings
from shared.db import AutoLinkNotice, BotAdmin, Channel, MembersRegistry, get_session
from shared.gateway import interface as gateway
from shared.gateway.app import app as gateway_app
from shared.gateway.auto_links import record_notice
from shared.gateway.undo_hook import handle_undo_reaction

from tests.fake_gowa.app import app as fake_gowa_app
from tests.gowa_payloads import reaction_event

ADMIN = "919999922222@s.whatsapp.net"


def _sign(body: bytes) -> str:
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


async def _post_reaction(payload: dict) -> httpx.Response:
    body = json.dumps(payload).encode()
    transport = ASGITransport(app=gateway_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(
            "/webhook/gowa",
            content=body,
            headers={"X-Hub-Signature-256": _sign(body)},
        )


async def test_webhook_reaction_event_undoes_autolink_end_to_end():
    group = "t19-group@g.us"
    jid = "t19-participant@s.whatsapp.net"
    lid = "t19-lid@lid"

    async with get_session() as session:
        session.add(Channel(jid=group, kind="project"))
        session.add(MembersRegistry(wa_identity=jid, member_title="Aira", cms_member_id="cms-1"))
        session.add(MembersRegistry(wa_identity=lid, member_title="Aira", cms_member_id="cms-1"))
        await session.commit()

    await record_notice("t19-msg", group, "Aira", "cms-1", [jid, lid])

    gateway.clear_hooks()
    gateway.register_reaction_hook(wa_agent.handle_reaction)
    gateway.register_reaction_hook(handle_undo_reaction)
    try:
        payload = reaction_event("t19-evt", group, ADMIN, "\u274c", "t19-msg")
        resp = await _post_reaction(payload)
        assert resp.status_code == 200
    finally:
        gateway.clear_hooks()

    async with get_session() as session:
        assert await session.get(MembersRegistry, jid) is None
        assert await session.get(MembersRegistry, lid) is None
        notice = await session.get(AutoLinkNotice, "t19-msg")
        assert notice is not None
        assert notice.status == "undone"

    assert len(fake_gowa_app.state.sent_messages) == 1
    sent = fake_gowa_app.state.sent_messages[0]
    assert sent.get("phone") == group
    assert "Undone" in sent.get("message", "")


async def test_webhook_reaction_in_unallowlisted_group_is_dropped():
    group = "t20-group@g.us"
    jid = "t20-participant@s.whatsapp.net"
    lid = "t20-lid@lid"

    # Notice: Channel row is NOT added for this group.
    async with get_session() as session:
        session.add(MembersRegistry(wa_identity=jid, member_title="Aira", cms_member_id="cms-1"))
        session.add(MembersRegistry(wa_identity=lid, member_title="Aira", cms_member_id="cms-1"))
        await session.commit()

    await record_notice("t20-msg", group, "Aira", "cms-1", [jid, lid])

    gateway.clear_hooks()
    gateway.register_reaction_hook(wa_agent.handle_reaction)
    gateway.register_reaction_hook(handle_undo_reaction)
    try:
        # A reaction in a non-allowlisted group is dropped before any hook runs.
        payload = reaction_event("t20-evt", group, ADMIN, "\u274c", "t20-msg")
        await _post_reaction(payload)
    finally:
        gateway.clear_hooks()

    async with get_session() as session:
        assert await session.get(MembersRegistry, jid) is not None
        assert await session.get(MembersRegistry, lid) is not None
        notice = await session.get(AutoLinkNotice, "t20-msg")
        assert notice is not None
        assert notice.status == "active"

    assert len(fake_gowa_app.state.sent_messages) == 0
