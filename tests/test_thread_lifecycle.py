"""Phase 4 stale/revival/ended acceptance criteria (docs/Plan - Watcher
v1.md): a thread with no activity for the stale window flips to stale
and is revivable; a human `state: ended` moves the file to the archive
on the next run; the Sunday job posts counts + up to 10 Lapis links."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from agents.wa_agent import interface as wa_agent
from httpx import ASGITransport
from shared.db import BotAdmin, Channel, get_session
from shared.gateway import interface as gateway
from shared.wiki.interface import LocalDirClient, PageNotFound, parse_page

from tests.fake_gowa.app import app as fake_gowa_app
from tests.gowa_payloads import message_event
from tests.scripted_models import ScriptedStructuredWorker


def _thread_page(slug: str, state: str, last_message_at: str, extra_sections: str = "") -> str:
    return (
        f"---\ntype: thread\nslug: {slug}\nchannel: \"[[Proj (WhatsApp)]]\"\ntitle: {slug}\n"
        f"state: {state}\nlast_message_at: {last_message_at}\n---\n# {slug}\n"
        "## Summary\n<!-- watcher:managed -->\nold\n<!-- /watcher -->\n"
        "## Items\n<!-- watcher:managed -->\n<!-- /watcher -->\n"
        "## Timeline\n<!-- watcher:append -->\n- old line [src:: x]\n<!-- /watcher -->\n"
        f"## Notes\n{extra_sections}"
    )


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


async def test_check_and_mark_stale_flips_old_active_thread(vault: LocalDirClient):
    now = datetime.now(UTC)
    old = (now - timedelta(days=10)).isoformat()
    recent = (now - timedelta(hours=1)).isoformat()

    await vault.create(
        "channels/proj/old-thread.md", _thread_page("old-thread", "active", old))
    await vault.create(
        "channels/proj/fresh-thread.md", _thread_page("fresh-thread", "active", recent))

    marked = await wa_agent.check_and_mark_stale(vault, "proj", now=now)
    assert marked == ["old-thread"]

    old_page = parse_page((await vault.read("channels/proj/old-thread.md")).content)
    fresh_page = parse_page((await vault.read("channels/proj/fresh-thread.md")).content)
    assert old_page.frontmatter.state == "stale"
    assert fresh_page.frontmatter.state == "active"


async def test_stale_thread_is_revived_by_a_new_message(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 1)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    await vault.create(
        "channels/revive/stale-thread.md", _thread_page("stale-thread", "stale", old))

    async with get_session() as session:
        session.add(Channel(jid="revive-chan@g.us", kind="project", title="Revive", initiative="Revive"))
        await session.commit()

    import json

    from shared.db import MessageBuffer

    async with get_session() as session:
        session.add(
            MessageBuffer(
                message_id="rev-m0",
                channel="revive-chan@g.us",
                event_type="message",
                payload=json.dumps(
                    message_event("rev-m0", "revive-chan@g.us", "reviving this", sender="x")
                ),
            )
        )
        await session.commit()

    worker = ScriptedStructuredWorker(
        route={"rev-m0": "stale-thread"}, title="stale-thread", summary="revived thread content"
    )
    result = await wa_agent.run_batch("revive-chan@g.us", vault, worker)
    assert result is not None
    assert result.threads_revived == ["stale-thread"]

    revived_page = parse_page((await vault.read("channels/revive/stale-thread.md")).content)
    assert revived_page.frontmatter.state == "active"


async def test_archive_ended_threads_moves_file(vault: LocalDirClient):
    ended_at = datetime.now(UTC).isoformat()
    await vault.create(
        "channels/proj2/ended-thread.md", _thread_page("ended-thread", "ended", ended_at))

    archived = await wa_agent.archive_ended_threads(vault, "proj2")
    assert archived == ["ended-thread"]

    with pytest.raises(PageNotFound):
        await vault.read("channels/proj2/ended-thread.md")

    archived_content = (await vault.read("channels/archive/proj2/ended-thread.md")).content
    assert "ended-thread" in archived_content


async def test_sunday_nudge_posts_counts_and_links(vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch):
    async with get_session() as session:
        session.add(BotAdmin(wa_identity="admin@x"))
        coordis = Channel(jid="coordis-nudge@g.us", kind="coordis")
        session.add(coordis)
        session.add(Channel(jid="nudge-proj@g.us", kind="project", title="NudgeProj"))
        await session.commit()

    # Pin the destination explicitly - the shared test DB persists across
    # the whole suite, and other test files also seed "coordis"-kind
    # channels, so a plain `get_channel_by_kind("coordis")` lookup could
    # return one of theirs depending on run order.
    async def _this_coordis(kind: str):
        return coordis

    monkeypatch.setattr(wa_agent, "get_channel_by_kind", _this_coordis)

    now = datetime.now(UTC)
    old = (now - timedelta(days=10)).isoformat()
    for i in range(12):
        await vault.create(
            f"channels/nudgeproj/stale-{i}.md",
            _thread_page(f"stale-{i}", "stale", old))

    text = await wa_agent.sunday_stale_nudge(vault, now=now)
    assert text is not None
    assert "12 stale threads" in text
    assert text.count("lapis.dvenom.in") == 10  # capped at 10 links

    assert fake_gowa_app.state.sent_messages[-1]["phone"] == "coordis-nudge@g.us"


async def test_sunday_nudge_noop_without_coordis_channel(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    # Patched directly rather than relying on "no coordis channel exists
    # anywhere" - the shared test DB persists across the whole suite, so
    # other test files' coordis channels would make that untestable here.
    async def _no_coordis(kind: str):
        return None

    monkeypatch.setattr(wa_agent, "get_channel_by_kind", _no_coordis)
    text = await wa_agent.sunday_stale_nudge(vault)
    assert text is None
