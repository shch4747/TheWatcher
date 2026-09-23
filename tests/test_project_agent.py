"""Phase 5 Project Agent v1 acceptance criteria (docs/Plan - Watcher
v1.md): project page tasks reflect chat within one batch of an Update
Notice; a human-ticked box is never overwritten by the next
regeneration; confirmed Proposals execute identically for Events and
Projects (already true via the WhatsApp Agent's confirm_proposal, exercised
in tests/test_chat_agent.py - not re-tested here)."""
from __future__ import annotations

from pathlib import Path

import pytest
from agents.project_agent import interface as project_agent
from shared.db import Channel, get_session
from shared.inbox.interface import THREAD_UPDATE, Inbox, InboxPost
from shared.models.text import TextModelClient
from shared.wiki.interface import LocalDirClient, parse_page

INITIATIVE_PAGE = (
    "---\ntype: project\nslug: watcher\ntitle: Watcher\nstatus: active\n"
    'lead: "[[Devansh]]"\n---\n# Watcher\n'
    "## Brief\nBot project.\n"
    "## Status\n<!-- watcher:managed -->\nold status\n<!-- /watcher -->\n"
    "## Open tasks\n<!-- watcher:managed -->\n<!-- /watcher -->\n"
    "## Decisions\n<!-- watcher:append -->\n<!-- /watcher -->\n"
    "## Log\n<!-- watcher:append -->\n<!-- /watcher -->\n"
    "## Resources\n<!-- watcher:append -->\n<!-- /watcher -->\n"
    "## Threads\n<!-- watcher:derived -->\n<!-- /watcher -->\n"
    "## Notes\n"
)

EVENT_PAGE = (
    "---\ntype: event\nslug: demo-day\ntitle: Demo Day\nstatus: active\n"
    'lead: "[[Devansh]]"\n---\n# Demo Day\n'
    "## Brief\nEvent.\n"
    "## Status\n<!-- watcher:managed -->\nold status\n<!-- /watcher -->\n"
    "## Logistics\n<!-- watcher:managed -->\n<!-- /watcher -->\n"
    "## Decisions\n<!-- watcher:append -->\n<!-- /watcher -->\n"
    "## Log\n<!-- watcher:append -->\n<!-- /watcher -->\n"
    "## Resources\n<!-- watcher:append -->\n<!-- /watcher -->\n"
    "## Threads\n<!-- watcher:derived -->\n<!-- /watcher -->\n"
    "## Notes\n"
)

THREAD_WITH_ITEMS = (
    '---\ntype: thread\nslug: t1\nchannel: "[[X (WhatsApp)]]"\ntitle: T1\nstate: active\n---\n# T1\n'
    "## Summary\n<!-- watcher:managed -->\nsummary\n<!-- /watcher -->\n"
    "## Items\n<!-- watcher:managed -->\n"
    "- [ ] Book the hall [kind:: task] [owner:: [[Aira]]] [due:: 2026-09-24] [src:: m1] ^i-0001\n"
    "- [x] Demo moves to Friday [kind:: decision] [src:: m2] ^i-0002\n"
    "- https://example.com/slides [kind:: resource] [src:: m3] ^i-0003\n"
    "- Is the hall booked? [kind:: question] [src:: m4] ^i-0004\n"
    "<!-- /watcher -->\n## Timeline\n<!-- watcher:append -->\n- line [src:: m1]\n"
    "<!-- /watcher -->\n## Notes\n"
)


class ScriptedWorker(TextModelClient):
    def __init__(self, text: str = "New status text."):
        self.model_name = "scripted"
        self.text = text

    async def generate(self, prompt: str, system: str | None = None):  # type: ignore[override]
        from shared.models.schemas import TextResult

        return TextResult(text=self.text, input_tokens=1, output_tokens=1)


@pytest.fixture
def vault(tmp_path: Path) -> LocalDirClient:
    return LocalDirClient(tmp_path)


async def test_apply_thread_items_upserts_by_kind(vault: LocalDirClient):
    await vault.create("channels/x/t1.md", THREAD_WITH_ITEMS)
    await vault.create("projects/watcher.md", INITIATIVE_PAGE)

    result = await project_agent.apply_thread_items_to_initiative(
        vault, "channels/x/t1.md", "projects/watcher.md"
    )
    assert result.tasks_upserted == 1
    assert result.decisions_appended == 1
    assert result.resources_appended == 1
    assert result.log_lines_appended == 1

    page = parse_page((await vault.read("projects/watcher.md")).content)
    assert "Book the hall" in page.section("Open tasks").body
    assert "Demo moves to Friday" in page.section("Decisions").body
    assert "example.com/slides" in page.section("Resources").body
    assert "Is the hall booked?" in page.section("Log").body


async def test_human_ticked_box_survives_reapplication(vault: LocalDirClient):
    await vault.create("channels/x/t1.md", THREAD_WITH_ITEMS)
    await vault.create("projects/watcher.md", INITIATIVE_PAGE)

    await project_agent.apply_thread_items_to_initiative(vault, "channels/x/t1.md", "projects/watcher.md")

    # human ticks the box in the wiki
    current = await vault.read("projects/watcher.md")
    page = parse_page(current.content)
    ticked_body = page.section("Open tasks").body.replace("[ ]", "[x]", 1)
    from shared.wiki.interface import Section

    new_sections = [
        Section(
            title=s.title,
            header_raw=s.header_raw,
            body=ticked_body if s.title == "Open tasks" else s.body,
        )
        for s in page.sections
    ]
    from dataclasses import replace

    ticked_page = replace(page, sections=new_sections)
    from shared.wiki.interface import dump_page

    await vault.write("projects/watcher.md", dump_page(ticked_page), base_revision=current.revision)

    # re-apply the same thread items (as if the batch ran again)
    await project_agent.apply_thread_items_to_initiative(vault, "channels/x/t1.md", "projects/watcher.md")

    final_page = parse_page((await vault.read("projects/watcher.md")).content)
    assert "[x] Book the hall" in final_page.section("Open tasks").body


async def test_event_uses_logistics_section(vault: LocalDirClient):
    await vault.create("channels/x/t1.md", THREAD_WITH_ITEMS)
    await vault.create("events/demo-day.md", EVENT_PAGE)

    result = await project_agent.apply_thread_items_to_initiative(
        vault, "channels/x/t1.md", "events/demo-day.md"
    )
    assert result.tasks_upserted == 1

    page = parse_page((await vault.read("events/demo-day.md")).content)
    assert "Book the hall" in page.section("Logistics").body


async def test_rewrite_status_replaces_managed_section(vault: LocalDirClient):
    await vault.create("projects/watcher.md", INITIATIVE_PAGE)
    await project_agent.rewrite_status(vault, "projects/watcher.md", ScriptedWorker("Great progress."))

    page = parse_page((await vault.read("projects/watcher.md")).content)
    assert "Great progress." in page.section("Status").body
    assert "old status" not in page.section("Status").body


async def test_applying_the_same_thread_twice_changes_nothing(vault: LocalDirClient):
    """Regression: every notice re-appended every past decision, resource
    and question. Items carry block ids; a second apply is a no-op."""
    await vault.create("channels/x/t1.md", THREAD_WITH_ITEMS)
    await vault.create("projects/watcher.md", INITIATIVE_PAGE)

    await project_agent.apply_thread_items_to_initiative(vault, "channels/x/t1.md", "projects/watcher.md")
    once = (await vault.read("projects/watcher.md")).content
    again = await project_agent.apply_thread_items_to_initiative(
        vault, "channels/x/t1.md", "projects/watcher.md"
    )
    assert (await vault.read("projects/watcher.md")).content == once
    assert (again.decisions_appended, again.resources_appended, again.log_lines_appended) == (0, 0, 0)


async def _seed_e2e(vault: LocalDirClient, jid: str, name: str) -> None:
    async with get_session() as session:
        session.add(Channel(jid=jid, kind="project", title=name, initiative=name))
        await session.commit()
    slug = name.lower()
    initiative_page = INITIATIVE_PAGE.replace("slug: watcher", f"slug: {slug}").replace(
        "title: Watcher", f"title: {name}"
    )
    await vault.create(f"projects/{slug}.md", initiative_page)
    await vault.create(f"channels/{slug}/t1.md", THREAD_WITH_ITEMS)
    await Inbox(vault, "project_agent").post(
        InboxPost(kind=THREAD_UPDATE, text="Thread update: T1", channel=jid, thread_slug="t1")
    )


async def test_run_project_agent_once_end_to_end(vault: LocalDirClient):
    await _seed_e2e(vault, "pa-e2e-chan@g.us", "PaE2e")

    results = await project_agent.run_project_agent_once(vault, ScriptedWorker())
    assert len(results) == 1
    assert results[0].tasks_upserted == 1

    page = parse_page((await vault.read("projects/pae2e.md")).content)
    assert "Book the hall" in page.section("Open tasks").body
    assert "New status text." in page.section("Status").body
    assert await Inbox(vault, "project_agent").pending() == []


async def test_a_failed_apply_stays_in_the_inbox_and_is_retried(vault: LocalDirClient):
    await _seed_e2e(vault, "pa-retry-chan@g.us", "PaRetry")

    class Broken(ScriptedWorker):
        async def generate(self, prompt, system=None):  # type: ignore[override]
            raise RuntimeError("provider down")

    assert await project_agent.run_project_agent_once(vault, Broken()) == []
    assert len(await Inbox(vault, "project_agent").pending()) == 1

    assert len(await project_agent.run_project_agent_once(vault, ScriptedWorker())) == 1
    assert await Inbox(vault, "project_agent").pending() == []
