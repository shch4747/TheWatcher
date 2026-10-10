"""Tests for /setup-members undo notice creation, rejection skipping, and failure handling."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport
from shared.cms.interface import SeedFileCmsClient
from shared.db import AutoLinkNotice, BotAdmin, MembersRegistry, RejectedLink, get_session
from shared.gateway import interface as gateway
from shared.gateway.runtime import GatewayRuntime
from shared.wiki.interface import LocalDirClient

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


@pytest.fixture(autouse=True)
def _patch_cms(monkeypatch):
    monkeypatch.setattr(
        "shared.gateway.command_handlers.default_cms_client",
        lambda: SeedFileCmsClient(Path(__file__).parent / "fixtures" / "cms" / "members_seed.json"),
    )


@pytest.fixture
def vault(tmp_path: Path) -> LocalDirClient:
    return LocalDirClient(tmp_path)


async def test_setup_members_creates_active_undo_notice(vault: LocalDirClient):
    group = "t8-group@g.us"
    jid = "t8-participant@s.whatsapp.net"
    lid = "t8-lid@lid"
    phone = "t8-phone"
    fake_gowa_app.state.group_participants = {
        group: [
            {
                "jid": jid,
                "display_name": "Aira J",
                "lid": lid,
                "phone_number": phone,
            }
        ]
    }

    reply = await gateway.handle_command(group, ADMIN, "/setup-members", vault)
    assert reply is not None
    assert len(fake_gowa_app.state.sent_messages) == 1
    sent = fake_gowa_app.state.sent_messages[0]
    msg_text = sent.get("message", "")
    assert "React" in msg_text and "undo" in msg_text

    msg_id = sent["message_id"]
    async with get_session() as session:
        notice = await session.get(AutoLinkNotice, msg_id)
    assert notice is not None
    assert notice.status == "active"
    assert notice.channel == group
    assert notice.member_title == "Aira"
    stored_identities = json.loads(notice.identities)
    assert jid in stored_identities
    assert lid in stored_identities


async def test_setup_members_skips_rejected_link_pair(vault: LocalDirClient):
    group = "t9-group@g.us"
    jid = "t9-participant@s.whatsapp.net"
    lid = "t9-lid@lid"
    phone = "t9-phone"

    async with get_session() as session:
        session.add(RejectedLink(wa_identity=jid, member_title="Aira"))
        await session.commit()

    fake_gowa_app.state.group_participants = {
        group: [
            {
                "jid": jid,
                "display_name": "Aira J",
                "lid": lid,
                "phone_number": phone,
            }
        ]
    }

    reply = await gateway.handle_command(group, ADMIN, "/setup-members", vault)
    assert reply is not None
    assert "undone before" in reply

    async with get_session() as session:
        reg = await session.get(MembersRegistry, jid)
    assert reg is None

    undo_sends = [
        m for m in fake_gowa_app.state.sent_messages if "undo" in m.get("message", "").lower()
    ]
    assert len(undo_sends) == 0


async def test_setup_members_handles_send_failure(monkeypatch: pytest.MonkeyPatch, vault: LocalDirClient):
    group = "t10-group@g.us"
    jid = "t10-participant@s.whatsapp.net"
    lid = "t10-lid@lid"
    phone = "t10-phone"

    async def _failing_send(*args, **kwargs):
        raise RuntimeError("send failure")

    monkeypatch.setattr(GatewayRuntime, "send", _failing_send)

    fake_gowa_app.state.group_participants = {
        group: [
            {
                "jid": jid,
                "display_name": "Aira J",
                "lid": lid,
                "phone_number": phone,
            }
        ]
    }

    reply = await gateway.handle_command(group, ADMIN, "/setup-members", vault)
    assert reply is not None
    assert "could not offer undo" in reply

    async with get_session() as session:
        reg_jid = await session.get(MembersRegistry, jid)
        reg_lid = await session.get(MembersRegistry, lid)
    assert reg_jid is not None and reg_jid.member_title == "Aira"
    assert reg_lid is not None and reg_lid.member_title == "Aira"


async def test_setup_members_handles_record_notice_failure(
    monkeypatch: pytest.MonkeyPatch, vault: LocalDirClient
):
    group = "t11-group@g.us"
    jid = "t11-participant@s.whatsapp.net"
    lid = "t11-lid@lid"
    phone = "t11-phone"

    async def _failing_record(*args, **kwargs):
        raise RuntimeError("record_notice failure")

    monkeypatch.setattr("shared.gateway.command_handlers.record_notice", _failing_record)

    fake_gowa_app.state.group_participants = {
        group: [
            {
                "jid": jid,
                "display_name": "Aira J",
                "lid": lid,
                "phone_number": phone,
            }
        ]
    }

    reply = await gateway.handle_command(group, ADMIN, "/setup-members", vault)
    assert reply is not None
    assert "could not offer undo" in reply

    async with get_session() as session:
        reg_jid = await session.get(MembersRegistry, jid)
        reg_lid = await session.get(MembersRegistry, lid)
    assert reg_jid is not None and reg_jid.member_title == "Aira"
    assert reg_lid is not None and reg_lid.member_title == "Aira"
