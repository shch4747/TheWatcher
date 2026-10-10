"""Tests for shared/gateway/undo_hook.py handling red-cross reactions."""
from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport
from shared.db import AutoLinkNotice, BotAdmin, MembersRegistry, RejectedLink, get_session
from shared.gateway import interface as gateway
from shared.gateway.auto_links import record_notice
from shared.gateway.undo_hook import handle_undo_reaction

from tests.fake_gowa.app import app as fake_gowa_app

ADMIN = "919999999999@s.whatsapp.net"


@pytest.fixture(autouse=True)
async def _seed_admin():
    async with get_session() as session:
        if await session.get(BotAdmin, ADMIN) is None:
            session.add(BotAdmin(wa_identity=ADMIN))
            await session.commit()


@pytest.fixture(autouse=True)
def _wire_fake_gowa(monkeypatch):
    """Commands can trigger an immediate send() (e.g. "watching") - point
    the gateway's outbound client at the fake gowa app, same as Phase 0."""
    fake_transport = ASGITransport(app=fake_gowa_app)
    fake_client = httpx.AsyncClient(transport=fake_transport, base_url="http://fake-gowa")
    monkeypatch.setattr(gateway._client, "_client", fake_client)
    monkeypatch.setattr(gateway._client, "base_url", "http://fake-gowa")
    fake_gowa_app.state.sent_messages = []


async def test_handle_undo_reaction_success():
    group = "t12-group@g.us"
    msg = "t12-msg"
    jid = "t12-participant@s.whatsapp.net"
    lid = "t12-lid@lid"

    async with get_session() as session:
        session.add(MembersRegistry(wa_identity=jid, member_title="Aira", cms_member_id="cms-1"))
        session.add(MembersRegistry(wa_identity=lid, member_title="Aira", cms_member_id="cms-1"))
        await session.commit()

    await record_notice(msg, group, "Aira", "cms-1", [jid, lid])

    result = await handle_undo_reaction(ADMIN, msg, "\u274c")
    assert result is not None
    assert "Undone" in result and "removed 2" in result

    async with get_session() as session:
        assert await session.get(MembersRegistry, jid) is None
        assert await session.get(MembersRegistry, lid) is None
        notice = await session.get(AutoLinkNotice, msg)
        assert notice is not None
        assert notice.status == "undone"
        assert await session.get(RejectedLink, {"wa_identity": jid, "member_title": "Aira"}) is not None

    assert len(fake_gowa_app.state.sent_messages) == 1
    sent = fake_gowa_app.state.sent_messages[0]
    assert sent.get("phone") == group
    assert "Undone" in sent.get("message", "")


async def test_handle_undo_reaction_variation_selector():
    group = "t13-group@g.us"
    msg = "t13-msg"
    jid = "t13-participant@s.whatsapp.net"
    lid = "t13-lid@lid"

    async with get_session() as session:
        session.add(MembersRegistry(wa_identity=jid, member_title="Aira", cms_member_id="cms-1"))
        session.add(MembersRegistry(wa_identity=lid, member_title="Aira", cms_member_id="cms-1"))
        await session.commit()

    await record_notice(msg, group, "Aira", "cms-1", [jid, lid])

    result = await handle_undo_reaction(ADMIN, msg, "\u274c\ufe0f")
    assert result is not None
    assert "Undone" in result and "removed 2" in result

    async with get_session() as session:
        assert await session.get(MembersRegistry, jid) is None
        assert await session.get(MembersRegistry, lid) is None
        notice = await session.get(AutoLinkNotice, msg)
        assert notice is not None
        assert notice.status == "undone"

    assert len(fake_gowa_app.state.sent_messages) == 1
    sent = fake_gowa_app.state.sent_messages[0]
    assert sent.get("phone") == group
    assert "Undone" in sent.get("message", "")


async def test_handle_undo_reaction_non_admin_ignored():
    group = "t14-group@g.us"
    msg = "t14-msg"
    jid = "t14-participant@s.whatsapp.net"
    lid = "t14-lid@lid"

    async with get_session() as session:
        session.add(MembersRegistry(wa_identity=jid, member_title="Aira", cms_member_id="cms-1"))
        session.add(MembersRegistry(wa_identity=lid, member_title="Aira", cms_member_id="cms-1"))
        await session.commit()

    await record_notice(msg, group, "Aira", "cms-1", [jid, lid])

    result = await handle_undo_reaction("t14-random@s.whatsapp.net", msg, "\u274c")
    assert result is None

    async with get_session() as session:
        assert await session.get(MembersRegistry, jid) is not None
        assert await session.get(MembersRegistry, lid) is not None
        notice = await session.get(AutoLinkNotice, msg)
        assert notice is not None
        assert notice.status == "active"

    assert len(fake_gowa_app.state.sent_messages) == 0


async def test_handle_undo_reaction_invalid_triggers_ignored():
    group = "t15-group@g.us"
    msg = "t15-msg"
    jid = "t15-participant@s.whatsapp.net"
    lid = "t15-lid@lid"

    async with get_session() as session:
        session.add(MembersRegistry(wa_identity=jid, member_title="Aira", cms_member_id="cms-1"))
        session.add(MembersRegistry(wa_identity=lid, member_title="Aira", cms_member_id="cms-1"))
        await session.commit()

    await record_notice(msg, group, "Aira", "cms-1", [jid, lid])

    assert await handle_undo_reaction(ADMIN, msg, "\U0001f44d") is None
    assert await handle_undo_reaction(ADMIN, msg, "") is None
    assert await handle_undo_reaction(ADMIN, None, "\u274c") is None

    assert len(fake_gowa_app.state.sent_messages) == 0

    async with get_session() as session:
        notice = await session.get(AutoLinkNotice, msg)
        assert notice is not None
        assert notice.status == "active"


async def test_handle_undo_reaction_second_attempt_returns_none():
    group = "t16-group@g.us"
    msg = "t16-msg"
    jid = "t16-participant@s.whatsapp.net"
    lid = "t16-lid@lid"

    async with get_session() as session:
        session.add(MembersRegistry(wa_identity=jid, member_title="Aira", cms_member_id="cms-1"))
        session.add(MembersRegistry(wa_identity=lid, member_title="Aira", cms_member_id="cms-1"))
        await session.commit()

    await record_notice(msg, group, "Aira", "cms-1", [jid, lid])

    first = await handle_undo_reaction(ADMIN, msg, "\u274c")
    assert first is not None

    second = await handle_undo_reaction(ADMIN, msg, "\u274c")
    assert second is None

    assert len(fake_gowa_app.state.sent_messages) == 1


async def test_handle_undo_reaction_send_failure_still_reverts(monkeypatch: pytest.MonkeyPatch):
    group = "t17-group@g.us"
    msg = "t17-msg"
    jid = "t17-participant@s.whatsapp.net"
    lid = "t17-lid@lid"

    async def _failing_send(*args, **kwargs):
        raise RuntimeError("send failure")

    monkeypatch.setattr("shared.gateway.undo_hook.send", _failing_send)

    async with get_session() as session:
        session.add(MembersRegistry(wa_identity=jid, member_title="Aira", cms_member_id="cms-1"))
        session.add(MembersRegistry(wa_identity=lid, member_title="Aira", cms_member_id="cms-1"))
        await session.commit()

    await record_notice(msg, group, "Aira", "cms-1", [jid, lid])

    result = await handle_undo_reaction(ADMIN, msg, "\u274c")
    assert result is not None
    assert "Undone" in result

    async with get_session() as session:
        assert await session.get(MembersRegistry, jid) is None
        assert await session.get(MembersRegistry, lid) is None
        notice = await session.get(AutoLinkNotice, msg)
        assert notice is not None
        assert notice.status == "undone"


async def test_handle_undo_reaction_keeps_changed_registry_row():
    group = "t18-group@g.us"
    msg = "t18-msg"
    jid = "t18-participant@s.whatsapp.net"
    lid = "t18-lid@lid"

    async with get_session() as session:
        session.add(MembersRegistry(wa_identity=jid, member_title="Aira", cms_member_id="cms-1"))
        session.add(MembersRegistry(wa_identity=lid, member_title="Dave Other", cms_member_id="cms-other"))
        await session.commit()

    await record_notice(msg, group, "Aira", "cms-1", [jid, lid])

    result = await handle_undo_reaction(ADMIN, msg, "\u274c")
    assert result is not None
    assert "removed 1" in result and "Kept 1" in result

    async with get_session() as session:
        assert await session.get(MembersRegistry, jid) is None
        lid_row = await session.get(MembersRegistry, lid)
        assert lid_row is not None
        assert lid_row.member_title == "Dave Other"
