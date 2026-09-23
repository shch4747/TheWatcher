"""The Thread Store: one Channel's Threads and its Channel Page, as the
wiki holds them (Wiki Format: thread and channel pages; Spec: thread
states).

Callers ask for Threads, never for paths. The store owns everything that
used to be re-derived at each call site: the directory a Channel's Threads
live in and its archive, which pages count as Threads in which state, how
a new Thread's id is minted (`<yyyymmdd>-<slug>`, unique against every
Thread the Channel has ever had), how a message id is found on a page,
how a Thread moves through `active -> stale -> active` and `ended ->
archive/`, how the Channel Page's index is regenerated, and moving all of
it when a Channel is retitled.

A store reads the Channel's thread pages once, on first use, and keeps
them current as it writes - one `run_batch` or lifecycle pass lists the
directory once, not once per operation. Make a new store per unit of
work; it does not notice pages changed behind its back (a write against
a stale revision still raises `ConflictError`, as everywhere).

Every page change goes through the page editor.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from shared.wiki.editor import append_lines, fenced_content, set_fenced, set_field, set_title
from shared.wiki.lapis_client import PageExists, VaultClient, read_if_exists
from shared.wiki.layout import (
    ChannelLike,
    archive_dir,
    channel_dir,
    channel_page_path,
    thread_dir,
)
from shared.wiki.parser import Page, dump_page, parse_page
from shared.wiki.templates import render_new_channel_page, render_new_thread_page, slugify
from shared.wiki.timeline import timeline_lines, timeline_src_ids

logger = logging.getLogger(__name__)

OPEN_STATES = ("active", "stale")


@dataclass
class StoredThread:
    slug: str
    path: str
    title: str
    state: str
    summary: str
    items_body: str
    timeline: list[str]
    message_ids: list[str]
    participants: list[str]
    last_message_at: datetime | None
    archived: bool = False
    revision: str = field(default="", repr=False)
    page: Page | None = field(default=None, repr=False)


@dataclass
class ThreadDraft:
    """A new Thread. `title` is final (already cleaned); the id is minted
    by the store from it and `opened_at`."""

    title: str
    summary: str
    items_body: str
    timeline_lines: list[str]
    participants: list[str]
    message_ids: list[str]
    opened_at: datetime
    last_message_at: datetime


@dataclass
class ThreadChange:
    """New material for an existing Thread: `summary`/`items_body` replace
    what's there, `timeline_lines`/`message_ids`/`participants` add to it.
    `title=None` keeps the current title."""

    summary: str
    items_body: str
    timeline_lines: list[str]
    message_ids: list[str]
    participants: list[str]
    last_message_at: datetime
    title: str | None = None


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=UTC)


def _to_thread(path: str, content: str, revision: str, archived: bool = False) -> StoredThread | None:
    try:
        page = parse_page(content)
    except Exception:  # noqa: BLE001 - a hand-broken page is lint's to report, not ours to crash on
        logger.warning("skipping unparseable page %s", path)
        return None
    if page.page_type != "thread":
        return None
    fm = page.frontmatter
    timeline = timeline_lines(fenced_content(page, "Timeline"))
    ids = list(dict.fromkeys([*(getattr(fm, "message_ids", None) or []),
                              *(i for line in timeline for i in timeline_src_ids(line))]))
    return StoredThread(
        slug=fm.slug,
        path=path,
        title=fm.title,
        state=getattr(fm, "state", "active"),
        summary=fenced_content(page, "Summary"),
        items_body=fenced_content(page, "Items"),
        timeline=timeline,
        message_ids=ids,
        participants=list(getattr(fm, "participants", None) or []),
        last_message_at=_aware(getattr(fm, "last_message_at", None)),
        archived=archived,
        revision=revision,
        page=page,
    )


def _index_line(thread: StoredThread) -> str:
    """Wiki Format §channel: `- [[channels/<dir>/<slug>|Title]] — last <date>`."""
    line = f"- [[{thread.path.removesuffix('.md')}|{thread.title}]]"
    if thread.last_message_at is not None:
        line += f" — last {thread.last_message_at.strftime('%Y-%m-%d')}"
    return line


class ThreadStore:
    def __init__(self, vault: VaultClient, channel: ChannelLike):
        self.vault = vault
        self.channel = channel
        self._threads: dict[str, StoredThread] | None = None
        self._archived: dict[str, StoredThread] | None = None

    # -- reading ------------------------------------------------------------

    async def _load(self, prefix: str, archived: bool) -> dict[str, StoredThread]:
        depth = prefix.count("/") + 1  # only pages directly in the directory
        out: dict[str, StoredThread] = {}
        for path in await self.vault.list(prefix):
            if path.count("/") != depth:
                continue
            result = await read_if_exists(self.vault, path)
            if result is None:
                continue
            thread = _to_thread(path, result.content, result.revision, archived)
            if thread is not None:
                out[thread.slug] = thread
        return out

    async def _live(self) -> dict[str, StoredThread]:
        if self._threads is None:
            self._threads = await self._load(thread_dir(self.channel), archived=False)
        return self._threads

    async def _archive(self) -> dict[str, StoredThread]:
        if self._archived is None:
            self._archived = await self._load(archive_dir(self.channel), archived=True)
        return self._archived

    async def threads(self, states: Iterable[str] = OPEN_STATES) -> list[StoredThread]:
        """Unarchived Threads in the given states, oldest id first. The
        default - active and stale - is what new messages may land in (a
        stale Thread is revivable)."""
        wanted = set(states)
        return sorted((t for t in (await self._live()).values() if t.state in wanted), key=lambda t: t.slug)

    async def get(self, slug: str) -> StoredThread | None:
        """A Thread by id, wherever it is - including ended and archived."""
        return (await self._live()).get(slug) or (await self._archive()).get(slug)

    async def find_by_src_id(self, src_id: str, states: Iterable[str] = OPEN_STATES) -> StoredThread | None:
        """The Thread a message id is on (exact match against the page's
        `message_ids` and every Timeline `[src:: ...]`)."""
        for thread in await self.threads(states):
            if src_id in thread.message_ids:
                return thread
        return None

    # -- writing ------------------------------------------------------------

    async def _taken_slugs(self) -> set[str]:
        return set(await self._live()) | set(await self._archive())

    async def create(self, draft: ThreadDraft) -> StoredThread:
        """Write a new Thread page. Its id is `<yyyymmdd>-<slug of title>`,
        suffixed `-2`, `-3`... if any Thread of this Channel - in any
        state, archived included - already has it, so a new Thread can
        never land on an existing page."""
        base = f"{draft.opened_at.strftime('%Y%m%d')}-{slugify(draft.title)}"
        taken = await self._taken_slugs()
        suffix = 1
        while True:
            slug = base if suffix == 1 else f"{base}-{suffix}"
            suffix += 1
            if slug in taken:
                continue
            path = f"{thread_dir(self.channel)}/{slug}.md"
            content = render_new_thread_page(
                slug=slug,
                title=draft.title,
                channel_title=self.channel.title or self.channel.jid,
                initiative=self.channel.initiative,
                summary=draft.summary,
                items_text=draft.items_body,
                timeline_lines=draft.timeline_lines,
                participants=draft.participants,
                message_ids=draft.message_ids,
                opened_at=draft.opened_at,
                last_message_at=draft.last_message_at,
            )
            try:
                result = await self.vault.create(path, content)
            except PageExists:
                continue  # written behind our back since we listed - take the next id
            thread = _to_thread(path, content, result.revision)
            assert thread is not None
            (await self._live())[slug] = thread
            return thread

    async def _write(self, thread: StoredThread, page: Page) -> StoredThread:
        content = dump_page(page)
        result = await self.vault.write(thread.path, content, base_revision=thread.revision)
        updated = _to_thread(thread.path, content, result.revision)
        assert updated is not None
        (await self._live())[updated.slug] = updated
        return updated

    async def revise(self, slug: str, change: ThreadChange) -> tuple[StoredThread, bool]:
        """Apply new material to a Thread. A stale Thread is revived (and
        `last_message_at` moves forward, which is what keeps it revived).
        Returns the updated Thread and whether it was revived."""
        thread = (await self._live())[slug]
        assert thread.page is not None
        page = thread.page
        if change.title is not None and change.title != thread.title:
            page = set_title(page, change.title)
        page = set_fenced(page, "Summary", change.summary)
        page = set_fenced(page, "Items", change.items_body)
        page = append_lines(page, "Timeline", change.timeline_lines)
        page = set_field(page, "message_ids", list(dict.fromkeys([
            *(getattr(page.frontmatter, "message_ids", None) or []), *change.message_ids])))
        page = set_field(page, "participants", sorted({*thread.participants, *change.participants}))
        page = set_field(page, "last_message_at", change.last_message_at)
        revived = thread.state == "stale"
        if revived:
            page = set_field(page, "state", "active")
        return await self._write(thread, page), revived

    async def mark_stale(self, now: datetime, after: timedelta) -> list[str]:
        """`active -> stale` for every Thread silent for `after`. Never
        touches `ended` - only a human ends a Thread."""
        marked = []
        for thread in await self.threads(("active",)):
            if thread.last_message_at is None or now - thread.last_message_at < after:
                continue
            assert thread.page is not None
            await self._write(thread, set_field(thread.page, "state", "stale"))
            marked.append(thread.slug)
        return marked

    async def archive_ended(self) -> list[str]:
        """Move every Thread a human set to `ended` into the archive
        directory. An archive page already holding the same id is never
        overwritten: identical content just removes the leftover, anything
        else is left in place and logged for a human."""
        archived = []
        for thread in await self.threads(("ended",)):
            content = dump_page(thread.page) if thread.page else ""
            target = f"{archive_dir(self.channel)}/{thread.slug}.md"
            try:
                result = await self.vault.create(target, content)
            except PageExists:
                existing = await read_if_exists(self.vault, target)
                if existing is None or existing.content != content:
                    logger.warning(
                        "not archiving %s: %s already exists with different content", thread.path, target
                    )
                    continue
                result = existing
            await self.vault.delete(thread.path)
            del (await self._live())[thread.slug]
            moved = _to_thread(target, content, result.revision, archived=True)
            if moved is not None:
                (await self._archive())[moved.slug] = moved
            archived.append(thread.slug)
        return archived

    # -- the Channel Page ---------------------------------------------------

    async def ensure_channel_page(self) -> bool:
        """Create the Channel Page if there isn't one. Never replaces an
        existing page (or the Notes a human wrote on it). Returns whether
        it created one."""
        content = render_new_channel_page(
            self.channel.title or self.channel.jid,
            self.channel.kind,
            slug=channel_dir(self.channel),
            initiative=self.channel.initiative,
        )
        try:
            await self.vault.create(channel_page_path(self.channel), content)
        except PageExists:
            return False
        return True

    async def reindex(self) -> None:
        """Rewrite the Channel Page's Active / Stale / Archived threads
        sections from what's in the vault. No Channel Page yet is a no-op."""
        path = channel_page_path(self.channel)
        existing = await read_if_exists(self.vault, path)
        if existing is None:
            return
        page = parse_page(existing.content)
        if page.page_type != "channel":
            return
        by_state = {state: [_index_line(t) for t in await self.threads((state,))] for state in OPEN_STATES}
        archived = [_index_line(t) for t in sorted((await self._archive()).values(), key=lambda t: t.slug)]
        page = set_fenced(page, "Active threads", "\n".join(by_state["active"]))
        page = set_fenced(page, "Stale threads", "\n".join(by_state["stale"]))
        page = set_fenced(page, "Archived threads", "\n".join(archived))
        await self.vault.write(path, dump_page(page), base_revision=existing.revision)

    # -- retitling ----------------------------------------------------------

    @classmethod
    async def relocate(cls, vault: VaultClient, old: ChannelLike, new: ChannelLike) -> list[str]:
        """A Channel's directory follows its title; when `/setup` retitles
        it, move its Thread pages, archive and Channel Page to the new
        directory instead of orphaning them. A page already at a target
        path is never overwritten (the old one stays and is reported).
        Returns the paths left behind."""
        if channel_dir(old) == channel_dir(new):
            return []
        moves: list[tuple[str, str]] = [(channel_page_path(old), channel_page_path(new))]
        dirs = ((thread_dir(old), thread_dir(new)), (archive_dir(old), archive_dir(new)))
        for source_dir, target_dir in dirs:
            depth = source_dir.count("/") + 1
            for path in await vault.list(source_dir):
                if path.count("/") == depth:
                    moves.append((path, f"{target_dir}/{path.rsplit('/', 1)[1]}"))
        left_behind = []
        for source, target in moves:
            existing = await read_if_exists(vault, source)
            if existing is None:
                continue
            content = existing.content
            if source == channel_page_path(old):
                page = set_title(parse_page(content), new.title or new.jid)
                content = dump_page(set_field(page, "slug", channel_dir(new)))
            try:
                await vault.create(target, content)
            except PageExists:
                left_behind.append(source)
                continue
            await vault.delete(source)
        return left_behind

