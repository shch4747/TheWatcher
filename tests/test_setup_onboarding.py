"""`/setup` seeds a new channel: it reads the chat history, hands the
channel to an ingest, and - for a project or event with no page yet - asks
the admin for context and creates the linked `projects/` or `events/`
page. The dialogue's answers are plain chat, so they must get through the
webhook before the channel is allowlisted."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from agents.wa_agent import interface as wa_agent
from httpx import ASGITransport
from shared.config import settings
from shared.db import BotAdmin, MessageBuffer, get_session
from shared.gateway import interface as gateway
from shared.gateway.app import app as gateway_app
from shared.gateway.commands import SetupCommand
from shared.gateway.onboarding import has_setup_session
from shared.wiki.interface import LocalDirClient, parse_page
from sqlalchemy import select

from tests.fake_gowa.app import app as fake_gowa_app
from tests.gowa_payloads import message_event
from tests.scripted_models import ScriptedStructuredWorker

ADMIN = "919999999999@s.whatsapp.net"


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
    fake_client = httpx.AsyncClient(transport=ASGITransport(app=fake_gowa_app), base_url="http://fake-gowa")
    monkeypatch.setattr(gateway._client, "_client", fake_client)
    monkeypatch.setattr(gateway._client, "base_url", "http://fake-gowa")
    fake_gowa_app.state.sent_messages = []
    fake_gowa_app.state.chat_history = {}
    gateway.clear_hooks()
    yield
    gateway.clear_hooks()


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalDirClient:
    client = LocalDirClient(tmp_path)
    # webhook-driven commands build their own vault client
    monkeypatch.setattr("shared.gateway.command_handlers.default_vault_client", lambda: client)
    return client


@pytest.fixture(autouse=True)
def _seed_roster(monkeypatch: pytest.MonkeyPatch):
    from shared.cms.interface import SeedFileCmsClient

    monkeypatch.setattr(
        "shared.gateway.onboarding.default_cms_client",
        lambda: SeedFileCmsClient(Path(__file__).parent / "fixtures" / "cms" / "members_seed.json"),
    )


def _history(jid: str, count: int) -> list[dict]:
    """gowa lists newest first."""
    now = datetime.now(UTC).replace(microsecond=0)
    return [
        {
            "id": f"{jid}-h{n}",
            "chat_jid": jid,
            "sender_jid": "x@s.whatsapp.net",
            "content": f"message {n}",
            "timestamp": (now - timedelta(minutes=count - n)).isoformat().replace("+00:00", "Z"),
        }
        for n in reversed(range(count))
    ]


async def _buffered(jid: str) -> list[MessageBuffer]:
    async with get_session() as session:
        rows = await session.scalars(
            select(MessageBuffer).where(MessageBuffer.channel == jid).order_by(MessageBuffer.id)
        )
        return list(rows)


async def _post(client: httpx.AsyncClient, message_id: str, jid: str, text: str, sender: str = ADMIN):
    body = json.dumps(message_event(message_id, jid, text, sender=sender)).encode()
    return await client.post("/webhook/gowa", content=body, headers={"X-Hub-Signature-256": _sign(body)})


# --- reading history ---------------------------------------------------


async def test_request_history_pages_past_gowas_limit_and_buffers_oldest_first():
    jid = "history-paging@g.us"
    fake_gowa_app.state.chat_history[jid] = _history(jid, 150)

    added = await gateway.request_history(jid, count=120)

    assert added == 120
    rows = await _buffered(jid)
    ids = [row.message_id for row in rows]
    # the newest 120 (h30..h149), chronologically
    assert ids == [f"{jid}-h{n}" for n in range(30, 150)]


async def test_setup_reads_history_and_notifies_the_channel_watched_hooks(vault: LocalDirClient):
    jid = "setup-history@g.us"
    fake_gowa_app.state.chat_history[jid] = _history(jid, 3)
    seen = []

    async def on_watched(channel):
        seen.append(channel)

    gateway.register_channel_watched_hook(on_watched)
    reply = await gateway.handle_command(jid, ADMIN, "/setup other Hackspace", vault)

    assert reply is not None
    assert reply.startswith("watching")
    assert "Read 3 message(s)" in reply
    assert [(c.jid, c.kind, c.title) for c in seen] == [(jid, "other", "Hackspace")]
    assert len(await _buffered(jid)) == 3


async def test_setup_with_no_history_does_not_start_an_ingest(vault: LocalDirClient):
    jid = "setup-empty-history@g.us"
    seen = []

    async def on_watched(channel):
        seen.append(channel)

    gateway.register_channel_watched_hook(on_watched)
    reply = await gateway.handle_command(jid, ADMIN, "/setup other", vault)

    assert reply is not None and reply.startswith("watching")
    assert "No earlier chat history" in reply
    assert seen == []


async def test_setup_survives_gowa_failing_to_return_history(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    jid = "setup-history-down@g.us"

    async def broken(*args, **kwargs):
        raise RuntimeError("gowa is down")

    monkeypatch.setattr(gateway._client, "get_chat_messages", broken)
    seen = []

    async def on_watched(channel):
        seen.append(channel)

    gateway.register_channel_watched_hook(on_watched)
    reply = await gateway.handle_command(jid, ADMIN, "/setup other", vault)

    assert reply is not None and reply.startswith("watching")
    assert "Couldn't read the chat history" in reply
    assert await gateway.get_channel(jid) is not None  # still watched
    assert seen == []


async def test_setup_logs_channel_never_reads_history(vault: LocalDirClient):
    existing = await gateway.get_channel_by_kind("logs")
    if existing is not None:
        await gateway.unwatch(existing.jid, ADMIN)
    jid = "setup-logs-history@g.us"
    fake_gowa_app.state.chat_history[jid] = _history(jid, 3)

    reply = await gateway.handle_command(jid, ADMIN, "/setup logs", vault)

    assert reply == "watching"
    assert await _buffered(jid) == []
    await gateway.unwatch(jid, ADMIN)


async def test_setup_of_an_existing_project_page_links_it_and_reads_history(vault: LocalDirClient):
    jid = "setup-existing-page@g.us"
    await vault.create("projects/old-timer.md", "existing page")
    fake_gowa_app.state.chat_history[jid] = _history(jid, 2)

    reply = await gateway.handle_command(jid, ADMIN, "/setup project Old Timer", vault)

    assert reply is not None and reply.startswith("watching")
    assert "Read 2 message(s)" in reply
    channel = await gateway.get_channel(jid)
    assert channel is not None and channel.initiative == "Old Timer"


# --- draining the first ingest -----------------------------------------


async def test_run_drained_ingests_a_backlog_bigger_than_one_batch(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    jid = "drain-backlog@g.us"
    monkeypatch.setattr(settings, "batch_n", 2)
    await gateway.setup(jid, ADMIN, SetupCommand(kind="other", title="Drain"), vault)
    fake_gowa_app.state.chat_history[jid] = _history(jid, 5)
    await gateway.request_history(jid, count=100)

    worker = ScriptedStructuredWorker(route={f"{jid}-h0": "new-1"}, default_route="chatter")
    result = await wa_agent.IngestionPipeline(vault, worker).run_drained(jid)

    assert result is not None
    assert result.message_count == 5
    assert len(result.threads_created) == 1
    assert await gateway.pending_messages(jid) == []
    assert await wa_agent.IngestionPipeline(vault, worker).run_drained(jid) is None


# --- the dialogue ------------------------------------------------------


async def test_dialogue_answers_get_through_the_webhook_before_the_channel_is_watched(
    vault: LocalDirClient,
):
    jid = "dialogue-webhook@g.us"
    await gateway.link_member(ADMIN, "Devansh")
    fake_gowa_app.state.chat_history[jid] = _history(jid, 2)
    async with httpx.AsyncClient(transport=ASGITransport(app=gateway_app), base_url="http://t") as client:
        assert (await _post(client, "dlg-1", jid, "/setup project Beacon")).status_code == 200
        assert await gateway.get_channel(jid) is None  # not watched until the dialogue ends
        assert (await _post(client, "dlg-2", jid, "me")).status_code == 200
        assert (await _post(client, "dlg-3", jid, "A beacon for lost keys")).status_code == 200
        assert (await _post(client, "dlg-4", jid, "prototype by December")).status_code == 200

    channel = await gateway.get_channel(jid)
    assert channel is not None
    assert (channel.kind, channel.initiative) == ("project", "Beacon")

    page = parse_page((await vault.read("projects/beacon.md")).content)
    assert page.frontmatter.lead == "[[Devansh]]"
    brief = page.section("Brief").body
    assert "A beacon for lost keys" in brief
    assert "**Timeline:** prototype by December" in brief

    sent = [m["message"] for m in fake_gowa_app.state.sent_messages if m["phone"] == jid]
    assert any("Who is the lead?" in m for m in sent)
    assert any("brief" in m.lower() for m in sent)
    assert any("timeline" in m.lower() for m in sent)
    assert any('"Beacon" created at projects/beacon' in m and "Read 2 message(s)" in m for m in sent)

    # the answers were setup input, not chat to be ingested - only the history is pending
    pending = await gateway.pending_messages(jid)
    assert sorted(m.message_id for m in pending) == [f"{jid}-h0", f"{jid}-h1"]


async def test_dialogue_ignores_other_people_in_the_channel(vault: LocalDirClient):
    jid = "dialogue-bystander@g.us"
    async with httpx.AsyncClient(transport=ASGITransport(app=gateway_app), base_url="http://t") as client:
        await _post(client, "bys-1", jid, "/setup event Hack Night")
        resp = await _post(client, "bys-2", jid, "who is this bot?", sender="918888888888@s.whatsapp.net")
        assert resp.status_code == 400  # filtered at the edge, not taken as the lead
    assert await has_setup_session(jid, ADMIN)
    assert not await has_setup_session(jid, "918888888888@s.whatsapp.net")


async def test_event_dialogue_creates_an_events_page(vault: LocalDirClient):
    jid = "dialogue-event@g.us"
    opening = await gateway.setup(jid, ADMIN, SetupCommand(kind="event", title="Hack Night"), vault)
    assert "lead" in opening.lower()
    prompt = await gateway.continue_setup_session(jid, "Aira J", vault)
    assert prompt is not None and "brief" in prompt.lower()
    prompt = await gateway.continue_setup_session(jid, "skip", vault)
    assert prompt is not None and "timeline" in prompt.lower()
    assert "happen" in prompt.lower()  # event wording
    reply = await gateway.continue_setup_session(jid, "Oct 20, 6pm", vault)

    assert reply is not None and "events/hack-night" in reply and "watching" in reply
    page = parse_page((await vault.read("events/hack-night.md")).content)
    assert page.frontmatter.lead == "[[Aira]]"  # matched to the roster
    assert page.section("Brief").body.strip() == "**Timeline:** Oct 20, 6pm"
    channel = await gateway.get_channel(jid)
    assert channel is not None and channel.kind == "event"


async def test_dialogue_lead_never_falls_back_to_a_jid(vault: LocalDirClient):
    jid = "dialogue-nolead@g.us"
    other = "918000000077@s.whatsapp.net"
    await gateway.setup(jid, ADMIN, SetupCommand(kind="project", title="Nameless"), vault)
    # a tag of someone who isn't linked to a member, and an unlinked admin saying "me"
    await gateway.continue_setup_session(jid, "@918000000077", vault, mentions=[other])
    await gateway.continue_setup_session(jid, "skip", vault)
    await gateway.continue_setup_session(jid, "skip", vault)

    page = (await vault.read("projects/nameless.md")).content
    assert "919999999999" not in page and "918000000077" not in page
    assert parse_page(page).frontmatter.lead == "[[Unassigned]]"


async def test_dialogue_lead_can_be_a_tagged_linked_member(vault: LocalDirClient):
    jid = "dialogue-tag@g.us"
    tagged = "919000000555@s.whatsapp.net"
    await gateway.link_member(tagged, "Zara Khan")
    await gateway.setup(jid, ADMIN, SetupCommand(kind="project", title="Tagged"), vault)
    await gateway.continue_setup_session(jid, "@919000000555", vault, mentions=[tagged])
    await gateway.continue_setup_session(jid, "skip", vault)
    await gateway.continue_setup_session(jid, "skip", vault)

    assert parse_page((await vault.read("projects/tagged.md")).content).frontmatter.lead == "[[Zara Khan]]"


async def test_a_new_setup_cancels_the_dialogue_it_replaces(vault: LocalDirClient):
    jid = "dialogue-cancel@g.us"
    await gateway.setup(jid, ADMIN, SetupCommand(kind="project", title="Abandoned"), vault)
    assert await has_setup_session(jid, ADMIN)

    assert await gateway.setup(jid, ADMIN, SetupCommand(kind="other", title="Plain"), vault) == "watching"

    assert not await has_setup_session(jid, ADMIN)
    assert await gateway.continue_setup_session(jid, "Devansh", vault) is None
    assert await vault.list("projects") == []
