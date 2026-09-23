"""Inboxes live on the wiki as `inbox/<agent>.md` (ADR-0014) and are only
ever touched through `Inbox`. Tests read the page back to check it is a
valid, human-readable inbox, and otherwise stay on the interface."""
from __future__ import annotations

from pathlib import Path

import pytest
from shared.inbox import interface as inbox_module
from shared.inbox.interface import THREAD_UPDATE, Inbox, InboxPost, register_consumer
from shared.scheduler.interface import RunAfter, due_jobs, register, request_run, unregister_all
from shared.wiki.interface import (
    ConflictError,
    LocalDirClient,
    append_lines,
    dump_page,
    fenced_content,
    parse_page,
    set_fenced,
)


@pytest.fixture
def vault(tmp_path: Path) -> LocalDirClient:
    return LocalDirClient(tmp_path)


@pytest.fixture(autouse=True)
def _clean():
    inbox_module.unregister_all()
    unregister_all()
    yield
    inbox_module.unregister_all()
    unregister_all()


def _notice(slug: str, since: str = "m1", trigger: bool = False) -> InboxPost:
    return InboxPost(
        kind=THREAD_UPDATE, text=f"Thread update: {slug}", channel="c@g.us", thread_slug=slug,
        thread_link=f"channels/c/{slug}", since=since, trigger=trigger,
    )


async def test_posting_creates_a_valid_inbox_page(vault):
    await Inbox(vault, "project_agent").post(_notice("t1"))
    page = parse_page((await vault.read("inbox/project_agent.md")).content)
    assert page.page_type == "inbox" and page.frontmatter.agent == "project_agent"
    line = fenced_content(page, "Pending")
    assert "[kind:: thread_update]" in line and "[thread:: [[channels/c/t1]]]" in line


async def test_pending_round_trips_what_was_posted(vault):
    inbox = Inbox(vault, "project_agent")
    await inbox.post(_notice("t1"), _notice("t2", since="m7"))
    items = await inbox.pending()
    assert [(i.thread_slug, i.since) for i in items] == [("t1", "m1"), ("t2", "m7")]


async def test_a_second_notice_for_the_same_thread_is_merged(vault):
    inbox = Inbox(vault, "project_agent")
    await inbox.post(_notice("t1", since="m1"))
    await inbox.post(_notice("t1", since="m9", trigger=True))
    [item] = await inbox.pending()
    assert item.since == "m1"  # the earliest new material wins
    assert item.trigger


async def test_ack_moves_items_to_done(vault):
    inbox = Inbox(vault, "project_agent")
    await inbox.post(_notice("t1"), _notice("t2"))
    first, second = await inbox.pending()
    await inbox.ack([first.id])
    assert [i.id for i in await inbox.pending()] == [second.id]
    done = fenced_content(parse_page((await vault.read(inbox.path)).content), "Done")
    assert f"^{first.id}" in done and "- [x]" in done and "[done::" in done


async def test_done_keeps_only_the_most_recent(vault, monkeypatch):
    monkeypatch.setattr(inbox_module, "DONE_KEPT", 3)
    inbox = Inbox(vault, "project_agent")
    for i in range(5):
        await inbox.post(_notice(f"t{i}"))
    await inbox.ack([i.id for i in await inbox.pending()])
    done = fenced_content(parse_page((await vault.read(inbox.path)).content), "Done").splitlines()
    assert len(done) == 3 and "t4" in done[-1]


async def test_a_line_a_human_typed_is_picked_up_and_can_be_acked(vault):
    inbox = Inbox(vault, "project_agent")
    await inbox.post(_notice("t1"))
    current = await vault.read(inbox.path)
    page = parse_page(current.content)
    body = fenced_content(page, "Pending") + "\n- [ ] Please re-check the budget"
    await vault.write(inbox.path, dump_page(set_fenced(page, "Pending", body)), current.revision)

    items = await inbox.pending()
    human = next(i for i in items if i.kind == "request")
    assert human.text == "Please re-check the budget"
    await inbox.ack([human.id])
    assert [i.thread_slug for i in await inbox.pending()] == ["t1"]


async def test_triggering_items_wake_the_consumer_and_routine_ones_do_not(vault):
    woken: list[str] = []
    register_consumer("project_agent", lambda: woken.append("project_agent"))
    inbox = Inbox(vault, "project_agent")

    await inbox.post(_notice("t1"))
    assert woken == []
    await inbox.post(_notice("t2", trigger=True))
    assert woken == ["project_agent"]
    assert "[trigger:: now]" in (await vault.read(inbox.path)).content


async def test_a_triggering_item_makes_the_agents_job_due_on_the_next_poll(vault):
    async def job() -> None: ...

    register("project_agent_tick", job, RunAfter("never-fired"))  # never due on its own
    assert "project_agent_tick" not in await due_jobs()

    register_consumer("project_agent", lambda: request_run("project_agent_tick"))
    await Inbox(vault, "project_agent").post(_notice("t1", trigger=True))
    assert "project_agent_tick" in await due_jobs()
    assert "project_agent_tick" not in await due_jobs()  # one request, one run


async def test_a_concurrent_edit_is_retried_not_lost(vault):
    inbox = Inbox(vault, "project_agent")
    await inbox.post(_notice("t1"))

    class RacingVault(LocalDirClient):
        raced = False

        async def write(self, path, content, base_revision):
            if not self.raced:  # a human edits Notes between our read and write
                self.raced = True
                current = await LocalDirClient.read(self, path)
                edited = append_lines(parse_page(current.content), "Notes", ["human note"])
                await LocalDirClient.write(self, path, dump_page(edited), current.revision)
            return await LocalDirClient.write(self, path, content, base_revision)

    racing = RacingVault(vault.root)
    await Inbox(racing, "project_agent").post(_notice("t2"))

    content = (await vault.read(inbox.path)).content
    assert "human note" in content
    assert [i.thread_slug for i in await inbox.pending()] == ["t1", "t2"]


async def test_persistent_conflicts_surface(vault):
    class AlwaysConflicts(LocalDirClient):
        async def write(self, path, content, base_revision):
            raise ConflictError(path, base_revision, "other")

    await Inbox(vault, "project_agent").post(_notice("t1"))
    with pytest.raises(ConflictError):
        await Inbox(AlwaysConflicts(vault.root), "project_agent").post(_notice("t2"))
