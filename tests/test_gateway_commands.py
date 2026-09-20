"""Phase 2 acceptance criteria (docs/Plan - Watcher v1.md):
/setup allowlists+creates/links pages and replies "watching"; a second
singleton /setup is refused; a missing initiative page opens a dialogue;
DMs and non-allowlisted groups are filtered at the edge."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport
from shared.config import settings
from shared.db import BotAdmin, Channel, MembersRegistry, get_session
from shared.gateway import interface as gateway
from shared.gateway.app import app as gateway_app
from shared.gateway.commands import (
    HealthCommand,
    LinkCommand,
    SetupCommand,
    StatusCommand,
    UnwatchCommand,
    parse_command,
)
from shared.wiki.interface import LocalDirClient
from sqlalchemy import select

from tests.fake_gowa.app import app as fake_gowa_app
from tests.gowa_payloads import message_event

ADMIN = "919999999999@s.whatsapp.net"
PROJECT_GROUP = "111-aaa@g.us"


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
    """Commands can trigger an immediate send() (e.g. "watching") - point
    the gateway's outbound client at the fake gowa app, same as Phase 0."""
    fake_transport = ASGITransport(app=fake_gowa_app)
    fake_client = httpx.AsyncClient(transport=fake_transport, base_url="http://fake-gowa")
    monkeypatch.setattr(gateway._client, "_client", fake_client)
    monkeypatch.setattr(gateway._client, "base_url", "http://fake-gowa")
    fake_gowa_app.state.sent_messages = []


@pytest.fixture
def vault(tmp_path: Path) -> LocalDirClient:
    return LocalDirClient(tmp_path)


def test_parse_command_recognizes_all_shapes():
    assert parse_command("/setup project Watcher") == SetupCommand(kind="project", title="Watcher")
    assert parse_command("/setup coordis") == SetupCommand(kind="coordis", title=None)
    assert parse_command("/unwatch") == UnwatchCommand()
    assert parse_command("/status") == StatusCommand()
    assert parse_command("/link 919876543210@s.whatsapp.net [[Aira]]") == LinkCommand(
        sender_ref="919876543210@s.whatsapp.net", member_ref="[[Aira]]"
    )
    assert parse_command("/health") == HealthCommand()
    assert parse_command("not a command") is None


async def test_setup_project_with_existing_page_allowlists_and_watches(vault: LocalDirClient):
    await vault.write("projects/watcher.md", "existing page", base_revision="")
    reply = await gateway.setup(
        PROJECT_GROUP, ADMIN, SetupCommand(kind="project", title="Watcher"), vault
    )
    assert reply == "watching"

    channel = await gateway.get_channel(PROJECT_GROUP)
    assert channel is not None
    assert channel.kind == "project"
    assert channel.initiative == "Watcher"
    assert (await vault.read("channels/watcher.md")).content


async def test_setup_project_missing_page_opens_dialogue_then_creates_it(vault: LocalDirClient):
    first_reply = await gateway.setup(
        PROJECT_GROUP, ADMIN, SetupCommand(kind="project", title="New Thing"), vault
    )
    assert "lead" in first_reply.lower()

    r2 = await gateway.continue_setup_session(PROJECT_GROUP, "Devansh", vault)
    assert "brief" in r2.lower()
    r3 = await gateway.continue_setup_session(PROJECT_GROUP, "A cool new project", vault)
    assert "timeline" in r3.lower()
    r4 = await gateway.continue_setup_session(PROJECT_GROUP, "by December", vault)
    assert "watching" in r4.lower()

    page = (await vault.read("projects/new-thing.md")).content
    assert "lead" in page
    assert "Devansh" in page
    assert "A cool new project" in page
    channel = await gateway.get_channel(PROJECT_GROUP)
    assert channel.initiative == "New Thing"


async def test_second_setup_of_singleton_kind_is_refused(vault: LocalDirClient):
    first = await gateway.setup(
        "coordis-group-1@g.us", ADMIN, SetupCommand(kind="coordis", title=None), vault
    )
    assert first == "watching"

    second = await gateway.setup(
        "coordis-group-2@g.us", ADMIN, SetupCommand(kind="coordis", title=None), vault
    )
    assert "already set up" in second

    other_channel = await gateway.get_channel("coordis-group-2@g.us")
    assert other_channel is None


async def test_setup_from_non_admin_is_refused(vault: LocalDirClient):
    group = "333-nonadmin@g.us"
    reply = await gateway.setup(
        group, "918888888888@s.whatsapp.net", SetupCommand(kind="other", title=None), vault
    )
    assert "only bot admins" in reply.lower()
    assert await gateway.get_channel(group) is None


async def test_unwatch_and_status(vault: LocalDirClient):
    await gateway.setup(PROJECT_GROUP, ADMIN, SetupCommand(kind="other", title=None), vault)
    assert "kind=other" in await gateway.status(PROJECT_GROUP)

    reply = await gateway.unwatch(PROJECT_GROUP, ADMIN)
    assert "no longer watching" in reply.lower()
    assert "not watching" in (await gateway.status(PROJECT_GROUP)).lower()


async def test_health_reports_gowa_vault_and_counts():
    async with get_session() as session:
        session.add(BotAdmin(wa_identity="health-admin@x"))
        session.add(Channel(jid="health-chan@g.us", kind="project"))
        await session.commit()

    report = await gateway.health()
    assert "gowa:" in report
    assert "vault:" in report
    assert "decision model:" in report
    assert "worker model:" in report
    assert "bot admins:" in report
    assert "pending proposals:" in report


async def test_health_command_dispatches_through_handle_command(vault: LocalDirClient):
    reply = await gateway.handle_command(PROJECT_GROUP, ADMIN, "/health", vault)
    assert reply is not None
    assert "gowa:" in reply


async def test_link_writes_members_registry():
    cmd = LinkCommand(sender_ref="919876543210@s.whatsapp.net", member_ref="[[Aira]]")
    reply = await gateway.link(cmd, ADMIN)
    assert "Linked" in reply

    async with get_session() as session:
        row = await session.get(MembersRegistry, "919876543210@s.whatsapp.net")
    assert row is not None
    assert row.member_title == "Aira"
    assert row.linked_by == ADMIN


@pytest.fixture
def gateway_client():
    transport = ASGITransport(app=gateway_app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def test_dm_is_filtered_at_the_edge_even_from_admin(gateway_client):
    payload = message_event("wamid.DM1", ADMIN, "/status", sender=ADMIN)  # a DM jid, not a group
    body = json.dumps(payload).encode()
    async with gateway_client as client:
        resp = await client.post("/webhook/gowa", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert resp.status_code == 400


async def test_non_allowlisted_group_is_filtered_unless_admin_command(gateway_client):
    payload = message_event("wamid.NOTALLOWED", "999-unallowlisted@g.us", "just chatting")
    body = json.dumps(payload).encode()
    async with gateway_client as client:
        resp = await client.post("/webhook/gowa", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert resp.status_code == 400


async def test_admin_command_passes_edge_filter_before_allowlisting(gateway_client):
    payload = message_event("wamid.SETUPCMD", "999-unallowlisted@g.us", "/setup other", sender=ADMIN)
    body = json.dumps(payload).encode()
    async with gateway_client as client:
        resp = await client.post("/webhook/gowa", content=body, headers={"X-Hub-Signature-256": _sign(body)})
    assert resp.status_code == 200


async def test_edit_of_unprocessed_message_updates_buffer_in_place(gateway_client):
    channel = "222-bbb@g.us"
    async with get_session() as session:
        session.add(Channel(jid=channel, kind="project"))
        await session.commit()

    original = json.dumps(message_event("wamid.EDIT1", channel, "orig")).encode()
    edited = json.dumps(message_event("wamid.EDIT1", channel, "edited!", event="message.edited")).encode()
    async with gateway_client as client:
        headers1 = {"X-Hub-Signature-256": _sign(original)}
        await client.post("/webhook/gowa", content=original, headers=headers1)
        headers2 = {"X-Hub-Signature-256": _sign(edited)}
        resp = await client.post("/webhook/gowa", content=edited, headers=headers2)
    assert resp.status_code == 200

    from shared.db import MessageBuffer

    async with get_session() as session:
        row = await session.scalar(select(MessageBuffer).where(MessageBuffer.message_id == "wamid.EDIT1"))
    assert "edited!" in row.payload
    assert row.event_type == "message.edited"


async def test_send_failure_during_command_does_not_crash_webhook_or_strand_message(
    gateway_client, monkeypatch: pytest.MonkeyPatch
):
    """Regression: gowa rejecting the outbound reply (e.g. missing
    X-Device-Id) used to raise unhandled out of receive_webhook, 500ing
    the whole request and leaving the buffered message permanently
    unprocessed (the dedupe check short-circuits every gowa retry
    before the command ever runs again)."""

    async def _broken_send(*args, **kwargs):
        raise RuntimeError("gowa rejected the outbound send")

    monkeypatch.setattr(gateway, "send", _broken_send)

    payload = message_event("wamid.SENDFAIL", "999-sendfail@g.us", "/setup other", sender=ADMIN)
    body = json.dumps(payload).encode()
    async with gateway_client as client:
        resp = await client.post("/webhook/gowa", content=body, headers={"X-Hub-Signature-256": _sign(body)})

    assert resp.status_code == 200  # not 500 - the webhook itself succeeded

    from shared.db import MessageBuffer

    async with get_session() as session:
        row = await session.scalar(select(MessageBuffer).where(MessageBuffer.message_id == "wamid.SENDFAIL"))
        channel = await session.scalar(select(Channel).where(Channel.jid == "999-sendfail@g.us"))
    assert row.processed is True  # not stranded
    assert channel is not None  # the command still took effect
