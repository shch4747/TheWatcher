"""The Thread Store owns where a Channel's Threads live and how they move.
Tests go through its interface only - no test here builds a thread path
except to check where a page ended up."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from shared.wiki.interface import (
    LocalDirClient,
    ThreadChange,
    ThreadDraft,
    ThreadStore,
    dump_page,
    parse_page,
    read_if_exists,
    set_field,
)

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


@dataclass
class Chan:
    jid: str = "store@g.us"
    title: str | None = "Hall Team"
    kind: str = "project"
    initiative: str | None = "Hall"


@pytest.fixture
def vault(tmp_path: Path) -> LocalDirClient:
    return LocalDirClient(tmp_path)


def _draft(title: str = "Booking the hall", ids: list[str] | None = None, at: datetime = T0) -> ThreadDraft:
    ids = ids or ["m1"]
    return ThreadDraft(
        title=title, summary="About the hall.", items_body="", participants=["Aira"], message_ids=ids,
        timeline_lines=[f"- {at:%Y-%m-%d %H:%M} — Aira: text [src:: {', '.join(ids)}]"],
        opened_at=at, last_message_at=at,
    )


def _change(ids: list[str], at: datetime, title: str | None = None) -> ThreadChange:
    return ThreadChange(
        summary="Updated.", items_body="", message_ids=ids, participants=["Dev"], last_message_at=at,
        timeline_lines=[f"- {at:%Y-%m-%d %H:%M} — Dev: more [src:: {', '.join(ids)}]"], title=title,
    )


async def test_created_threads_are_found_by_exact_message_id(vault):
    store = ThreadStore(vault, Chan())
    await store.create(_draft(ids=["3EB0A1B2"]))
    assert (await store.find_by_src_id("3EB0A1B2")) is not None
    # a prefix of a real id is a different message, not a match
    assert await ThreadStore(vault, Chan()).find_by_src_id("3EB0A1") is None


async def test_new_ids_never_land_on_an_ended_or_archived_thread(vault):
    store = ThreadStore(vault, Chan())
    first = await store.create(_draft())
    await store.revise(first.slug, _change(["m2"], T0))
    current = await vault.read(first.path)
    ended = set_field(parse_page(current.content), "state", "ended")
    await vault.write(first.path, dump_page(ended), current.revision)
    store = ThreadStore(vault, Chan())
    assert await store.archive_ended() == [first.slug]

    again = await ThreadStore(vault, Chan()).create(_draft())
    assert again.slug == f"{first.slug}-2"
    assert await read_if_exists(vault, f"channels/archive/hall-team/{first.slug}.md") is not None


async def test_revise_appends_and_revives_a_stale_thread(vault):
    store = ThreadStore(vault, Chan())
    thread = await store.create(_draft())
    await store.mark_stale(T0 + timedelta(days=10), timedelta(days=3))
    assert (await ThreadStore(vault, Chan()).get(thread.slug)).state == "stale"

    store = ThreadStore(vault, Chan())
    change = _change(["m2"], T0 + timedelta(days=11), title="Hall is booked")
    updated, revived = await store.revise(thread.slug, change)
    assert revived and updated.state == "active"
    assert updated.title == "Hall is booked"
    assert updated.message_ids == ["m1", "m2"]
    assert updated.participants == ["Aira", "Dev"]
    assert len(updated.timeline) == 2
    assert await store.find_by_src_id("m2") is not None


async def test_reindex_lists_threads_by_state_on_the_channel_page(vault):
    store = ThreadStore(vault, Chan())
    assert await store.ensure_channel_page()
    assert not await store.ensure_channel_page()  # never replaces an existing page
    a = await store.create(_draft("Booking the hall"))
    b = await store.create(_draft("Ordering pizza"))
    await store.mark_stale(T0 + timedelta(days=10), timedelta(days=3))
    await store.revise(a.slug, _change(["m9"], T0 + timedelta(days=10)))
    await store.reindex()

    page = parse_page((await vault.read("channels/hall-team.md")).content)
    assert f"[[{a.path.removesuffix('.md')}|Booking the hall]]" in page.section("Active threads").body
    assert f"[[{b.path.removesuffix('.md')}|Ordering pizza]]" in page.section("Stale threads").body


async def test_retitling_a_channel_moves_its_threads_instead_of_orphaning_them(vault):
    old = Chan(title="Hall Team")
    store = ThreadStore(vault, old)
    await store.ensure_channel_page()
    thread = await store.create(_draft(ids=["m1"]))

    new = Chan(title="Venue Team")
    assert await ThreadStore.relocate(vault, old, new) == []

    moved = ThreadStore(vault, new)
    assert [t.slug for t in await moved.threads()] == [thread.slug]
    assert await moved.find_by_src_id("m1") is not None
    channel_page = parse_page((await vault.read("channels/venue-team.md")).content)
    assert channel_page.frontmatter.title == "Venue Team"
    assert await vault.list("channels/hall-team") == []
