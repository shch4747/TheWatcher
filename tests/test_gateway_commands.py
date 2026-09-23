"""Phase 2 acceptance criteria (docs/Plan - Watcher v1.md):
/setup allowlists+creates/links pages and replies "watching"; a second
singleton /setup is refused; a missing initiative page opens a dialogue;
DMs and non-allowlisted groups are filtered at the edge."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from agents.wa_agent import interface as wa_agent
from httpx import ASGITransport
from shared.config import settings
from shared.db import BotAdmin, Channel, MembersRegistry, get_session
from shared.gateway import interface as gateway
from shared.gateway.app import app as gateway_app
from shared.gateway.commands import (
    ChannelsCommand,
    HealthCommand,
    IngestCommand,
    LinkCommand,
    SetupCommand,
    StatusCommand,
    UnwatchCommand,
    parse_command,
)
from shared.scheduler.interface import RunEvery, register, unregister_all
from shared.wiki.interface import LocalDirClient, ThreadStore, append_lines, dump_page, parse_page
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
    assert parse_command("/setup logs") == SetupCommand(kind="logs", title=None)
    assert parse_command("/unwatch") == UnwatchCommand()
    assert parse_command("/status") == StatusCommand()
    assert parse_command("/link 919876543210@s.whatsapp.net [[Aira]]") == LinkCommand(
        sender_ref="919876543210@s.whatsapp.net", member_ref="[[Aira]]"
    )
    assert parse_command("/health") == HealthCommand()
    assert parse_command("/channels") == ChannelsCommand()
    assert parse_command("/ingest") == IngestCommand()
    assert parse_command("/unwatch 111-aaa@g.us") == UnwatchCommand(jid="111-aaa@g.us")
    assert parse_command("not a command") is None


async def test_setup_project_with_existing_page_allowlists_and_watches(vault: LocalDirClient):
    await vault.create("projects/watcher.md", "existing page")
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


async def test_logs_is_a_singleton_kind(vault: LocalDirClient):
    first = await gateway.setup("logs-group-1@g.us", ADMIN, SetupCommand(kind="logs", title=None), vault)
    assert first == "watching"

    second = await gateway.setup(
        "logs-group-2@g.us", ADMIN, SetupCommand(kind="logs", title=None), vault
    )
    assert "already set up" in second
    assert await gateway.get_channel("logs-group-2@g.us") is None


async def test_setup_singleton_kind_defaults_title_and_writes_channel_page(vault: LocalDirClient):
    """Previously coordis/exes/research/all/logs/other channels got no
    title (fell back to a raw-jid-derived directory name in Lapis, e.g.
    "120363406427444035-g-us") and no channels/<slug>.md page at all -
    only project/event channels did."""
    await _clear_singleton_channel("coordis")
    group = "coordis-default-title@g.us"
    reply = await gateway.setup(group, ADMIN, SetupCommand(kind="coordis", title=None), vault)
    assert reply == "watching"

    channel = await gateway.get_channel(group)
    assert channel is not None
    assert channel.title == "Coordis"

    page = (await vault.read("channels/coordis.md")).content
    assert "type: channel" in page
    assert '"Coordis"' in page
    assert "## Active threads" in page


async def test_setup_singleton_kind_repeat_backfill_does_not_clobber_page(vault: LocalDirClient):
    await _clear_singleton_channel("coordis")
    group = "coordis-backfill@g.us"
    await gateway.setup(group, ADMIN, SetupCommand(kind="coordis", title=None), vault)
    await vault.create(
        "channels/coordis/some-thread.md", "unrelated thread content")

    # re-running /setup coordis (e.g. to pick up this fix on an
    # already-registered channel) must not overwrite the existing page
    first_page = await vault.read("channels/coordis.md")
    await gateway.setup(group, ADMIN, SetupCommand(kind="coordis", title=None), vault)
    second_page = await vault.read("channels/coordis.md")
    assert first_page.content == second_page.content
    assert (await vault.read("channels/coordis/some-thread.md")).content == "unrelated thread content"


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

    gateway.clear_hooks()
    gateway.register_health_line(wa_agent.pending_proposals_line)  # what main.py wires
    try:
        report = await gateway.health()
    finally:
        gateway.clear_hooks()
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


async def test_channels_lists_watched_channels_only(vault: LocalDirClient):
    await gateway.setup(PROJECT_GROUP, ADMIN, SetupCommand(kind="other", title=None), vault)

    report = await gateway.channels_report(ADMIN)
    assert PROJECT_GROUP in report
    assert "kind=other" in report


async def test_channels_refuses_non_admin():
    report = await gateway.channels_report("918888888888@s.whatsapp.net")
    assert "only bot admins" in report.lower()


async def test_unwatch_by_jid_from_a_different_channel(vault: LocalDirClient):
    target = "444-remote@g.us"
    await gateway.setup(target, ADMIN, SetupCommand(kind="other", title=None), vault)
    assert (await gateway.status(target)) != "Not watching this channel."

    reply = await gateway.unwatch("555-elsewhere@g.us", ADMIN, target_jid=target)
    assert "no longer watching" in reply.lower()
    assert target in reply
    assert (await gateway.status(target)) == "Not watching this channel."


async def test_unwatch_by_jid_for_unknown_channel():
    reply = await gateway.unwatch("555-elsewhere@g.us", ADMIN, target_jid="999-nope@g.us")
    assert "wasn't being watched" in reply


async def test_channels_and_unwatch_dispatch_through_handle_command(vault: LocalDirClient):
    group = "666-listed@g.us"
    await gateway.setup(group, ADMIN, SetupCommand(kind="other", title=None), vault)

    listed = await gateway.handle_command(PROJECT_GROUP, ADMIN, "/channels", vault)
    assert group in listed

    removed = await gateway.handle_command(PROJECT_GROUP, ADMIN, f"/unwatch {group}", vault)
    assert "no longer watching" in removed.lower()
    assert (await gateway.status(group)) == "Not watching this channel."


async def test_ingest_command_reports_unregistered_job_cleanly(vault: LocalDirClient):
    reply = await gateway.trigger_ingest(ADMIN)
    assert "ingest_now" in reply

    dispatched = await gateway.handle_command(PROJECT_GROUP, ADMIN, "/ingest", vault)
    assert dispatched is not None
    assert "ingest_now" in dispatched or "triggered" in dispatched.lower()


async def test_ingest_command_refuses_non_admin():
    reply = await gateway.trigger_ingest("918888888888@s.whatsapp.net")
    assert "only bot admins" in reply.lower()


async def _clear_singleton_channel(kind: str) -> None:
    """coordis/logs are singletons and other tests in this module leave
    one set up in the shared test DB - clear it first so these tests
    can assert on a known channel state."""
    existing = await gateway.get_channel_by_kind(kind)
    if existing is not None:
        await gateway.unwatch(existing.jid, ADMIN)


async def test_notify_logs_returns_false_with_no_logs_channel():
    await _clear_singleton_channel("logs")
    assert await gateway.notify_logs("something failed") is False


async def test_notify_logs_posts_to_the_logs_channel_not_coordis(vault: LocalDirClient):
    await _clear_singleton_channel("logs")
    await _clear_singleton_channel("coordis")
    logs = "logs-notify@g.us"
    coordis = "coordis-should-not-receive@g.us"
    await gateway.setup(logs, ADMIN, SetupCommand(kind="logs", title=None), vault)
    await gateway.setup(coordis, ADMIN, SetupCommand(kind="coordis", title=None), vault)

    posted = await gateway.notify_logs("⚠️ ingest_tick failed: boom")
    assert posted is True

    sent = fake_gowa_app.state.sent_messages
    assert any(m["phone"] == logs and "boom" in m["message"] for m in sent)
    assert not any(m["phone"] == coordis and "boom" in m["message"] for m in sent)


async def test_health_reports_last_run_of_each_scheduled_job():
    from shared.db import Run

    async with get_session() as session:
        session.add(Run(job_name="ingest_tick", outcome="failure", error="ConnectionError: boom"))
        await session.commit()
        row = await session.scalar(select(Run).where(Run.job_name == "ingest_tick"))
        assert row is not None
        row.finished_at = row.started_at
        await session.commit()

    async def _job() -> None: ...

    unregister_all()
    register("ingest_tick", _job, RunEvery(timedelta(minutes=2)))
    register("lifecycle_tick", _job, RunEvery(timedelta(hours=24)))
    try:
        report = await gateway.health()
    finally:
        unregister_all()
    assert "ingest_tick: ❌" in report
    assert "boom" in report
    assert "lifecycle_tick: no runs yet" in report  # every registered job is listed


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


async def test_repeat_setup_keeps_the_channel_page_and_its_notes(vault: LocalDirClient):
    jid = "repeat-setup@g.us"
    await gateway.setup(jid, ADMIN, SetupCommand(kind="other", title="Hackspace"), vault)
    current = await vault.read("channels/hackspace.md")
    with_notes = append_lines(parse_page(current.content), "Notes", ["Keys are with the guard."])
    await vault.write("channels/hackspace.md", dump_page(with_notes), current.revision)

    assert await gateway.setup(jid, ADMIN, SetupCommand(kind="other", title="Hackspace"), vault) == "watching"
    assert "Keys are with the guard." in (await vault.read("channels/hackspace.md")).content


async def test_retitling_a_channel_moves_its_threads(vault: LocalDirClient):
    jid = "retitle-setup@g.us"
    await gateway.setup(jid, ADMIN, SetupCommand(kind="other", title="Old Name"), vault)
    await vault.create(
        "channels/old-name/20260901-hall.md",
        "---\ntype: thread\nslug: 20260901-hall\nchannel: \"[[Old Name]]\"\ntitle: Hall\n"
        "state: active\n---\n# Hall\n",
    )

    await gateway.setup(jid, ADMIN, SetupCommand(kind="other", title="New Name"), vault)

    channel = await gateway.get_channel(jid)
    assert [t.slug for t in await ThreadStore(vault, channel).threads()] == ["20260901-hall"]
    assert parse_page((await vault.read("channels/new-name.md")).content).frontmatter.title == "New Name"
    assert await vault.list("channels/old-name") == []
