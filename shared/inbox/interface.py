"""Inboxes (ADR-0014): what an agent still has to look at, kept on Lapis
as `inbox/<agent>.md` - the source of truth, not a mirror of a table.

Nothing else reads or writes an inbox page; every agent goes through
`Inbox(vault, agent)`:

- `post(*items)` adds Inbox Items to `## Pending`. An item about the same
  thing as one already pending (same kind, Channel and Thread) is merged
  into it instead of queued twice - the earliest `since` and any
  `trigger` survive.
- `pending()` is every item still to do, oldest first. A line a human
  typed into Pending by hand counts too (it's given an id on the next
  write).
- `ack(ids)` moves items to `## Done` (the most recent DONE_KEPT are
  kept). Ack only after the work succeeded: an item that isn't acked is
  still pending on the next run, so a failed apply is retried, never
  lost.

**Triggering items** (`trigger=True`) are work the agent should do now:
posting one calls the wake-up registered for that agent with
`register_consumer` - `main.py` wires it to "run the agent's job on the
next scheduler poll". Other items wait for the agent's own cadence. The
flag is also on the page (`[trigger:: now]`), so a human reading an
inbox can tell urgent work from routine.

Writes are read-modify-write against the page revision; a conflict (a
human editing the inbox, or two writers) is retried from a fresh read.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from pydantic import BaseModel

from shared.wiki.interface import (
    ConflictError,
    Item,
    PageExists,
    VaultClient,
    dump_page,
    fenced_content,
    format_item_line,
    inbox_page_path,
    new_page_sections,
    parse_item_line,
    parse_page,
    read_if_exists,
    set_fenced,
    yaml_str,
)

logger = logging.getLogger(__name__)

THREAD_UPDATE = "thread_update"
DONE_KEPT = 50
_WRITE_ATTEMPTS = 3
_CHECKBOX_RE = re.compile(r"^-\s+(?:\[[ xX]\]\s+)?")


class InboxPost(BaseModel):
    """Something for an agent to look at. For an Update Notice: `kind`
    THREAD_UPDATE, the Channel jid, the Thread's id and wiki link, and the
    first message id with new material (`since`)."""

    kind: str
    text: str
    trigger: bool = False
    channel: str | None = None
    thread_slug: str | None = None
    thread_link: str | None = None
    since: str | None = None


class InboxItem(InboxPost):
    id: str
    posted_at: datetime | None = None


# --------------------------------------------------------------------------
# consumers
# --------------------------------------------------------------------------

Wake = Callable[[], None]
_Change = Callable[[list[InboxItem], list[str]], tuple[list[InboxItem], list[str]]]
_consumers: dict[str, Wake] = {}


def register_consumer(agent: str, wake: Wake) -> None:
    """How to wake `agent` when a triggering item is posted for it."""
    _consumers[agent] = wake


def unregister_all() -> None:
    """Test-only."""
    _consumers.clear()


# --------------------------------------------------------------------------
# line <-> item
# --------------------------------------------------------------------------


def _plain(text: str) -> str:
    """Item text on one line, with no brackets that could read as fields."""
    return re.sub(r"\s+", " ", text).replace("[", "(").replace("]", ")").strip()


def _mint_id(seed: str) -> str:
    return "n-" + hashlib.sha1(seed.encode()).hexdigest()[:8]


def _to_line(item: InboxItem, done_at: datetime | None = None) -> str:
    fields = {"kind": item.kind}
    for key, value in (("channel", item.channel), ("slug", item.thread_slug), ("since", item.since)):
        if value:
            fields[key] = value
    if item.thread_link:
        fields["thread"] = f"[[{item.thread_link}]]"
    if item.trigger:
        fields["trigger"] = "now"
    if item.posted_at:
        fields["posted"] = item.posted_at.isoformat(timespec="seconds")
    if done_at:
        fields["done"] = done_at.isoformat(timespec="seconds")
    return format_item_line(Item(_plain(item.text), item.id, done_at is not None, fields))


def _from_line(line: str) -> InboxItem | None:
    line = line.strip()
    if not line.startswith("- "):
        return None
    parsed = parse_item_line(line)
    if parsed is None:  # typed by a human, no id or fields
        text = _CHECKBOX_RE.sub("", line)
        return InboxItem(id=_mint_id(text), kind="request", text=text) if text else None
    f = parsed.fields
    posted = None
    if f.get("posted"):
        try:
            posted = datetime.fromisoformat(f["posted"])
        except ValueError:
            posted = None
    return InboxItem(
        id=parsed.block_id,
        kind=f.get("kind", "request"),
        text=parsed.text,
        trigger=f.get("trigger") == "now",
        channel=f.get("channel"),
        thread_slug=f.get("slug"),
        thread_link=f["thread"].strip("[]") if f.get("thread") else None,
        since=f.get("since"),
        posted_at=posted,
    )


def _same_subject(a: InboxPost, b: InboxPost) -> bool:
    return (
        a.kind == b.kind
        and a.thread_slug is not None
        and (a.channel, a.thread_slug) == (b.channel, b.thread_slug)
    )


# --------------------------------------------------------------------------
# the inbox
# --------------------------------------------------------------------------


class Inbox:
    def __init__(self, vault: VaultClient, agent: str):
        self.vault = vault
        self.agent = agent
        self.path = inbox_page_path(agent)

    def _new_page(self) -> str:
        return (
            "---\n"
            "type: inbox\n"
            f"slug: {self.agent}\n"
            f"title: {yaml_str(f'Inbox: {self.agent}')}\n"
            f"agent: {self.agent}\n"
            "---\n"
            f"# Inbox: {self.agent}\n"
        ) + new_page_sections("inbox")

    async def _read(self) -> tuple[list[InboxItem], list[str], str | None, str]:
        """(pending items, done lines, revision or None if no page, content)."""
        result = await read_if_exists(self.vault, self.path)
        if result is None:
            return [], [], None, self._new_page()
        page = parse_page(result.content)
        pending = [i for i in map(_from_line, fenced_content(page, "Pending").splitlines()) if i]
        done = [line for line in fenced_content(page, "Done").splitlines() if line.strip()]
        return pending, done, result.revision, result.content

    async def _write(self, change: _Change) -> None:
        for attempt in range(_WRITE_ATTEMPTS):
            pending, done, revision, content = await self._read()
            new_pending, new_done = change(pending, done)
            page = set_fenced(parse_page(content), "Pending", "\n".join(_to_line(i) for i in new_pending))
            page = set_fenced(page, "Done", "\n".join(new_done[-DONE_KEPT:]))
            try:
                if revision is None:
                    await self.vault.create(self.path, dump_page(page))
                else:
                    await self.vault.write(self.path, dump_page(page), base_revision=revision)
                return
            except (ConflictError, PageExists):
                if attempt == _WRITE_ATTEMPTS - 1:
                    raise
                logger.info("inbox %s changed under us; retrying", self.path)

    async def pending(self) -> list[InboxItem]:
        return (await self._read())[0]

    async def post(self, *posts: InboxPost) -> None:
        if not posts:
            return
        now = datetime.now(UTC)

        def change(pending: list[InboxItem], done: list[str]):
            for post in posts:
                existing = next((i for i in pending if _same_subject(i, post)), None)
                if existing is not None:
                    existing.text = post.text
                    existing.trigger = existing.trigger or post.trigger
                    existing.thread_link = post.thread_link or existing.thread_link
                    existing.since = existing.since or post.since
                    continue
                seed = f"{post.kind}|{post.channel}|{post.thread_slug}|{post.text}|{now.isoformat()}"
                pending.append(InboxItem(**post.model_dump(), id=_mint_id(seed), posted_at=now))
            return pending, done

        await self._write(change)
        if any(p.trigger for p in posts):
            wake = _consumers.get(self.agent)
            if wake is None:
                logger.warning("triggering item for %s but no consumer is registered", self.agent)
            else:
                wake()

    async def ack(self, ids: Iterable[str]) -> None:
        wanted = set(ids)
        if not wanted:
            return
        now = datetime.now(UTC)

        def change(pending: list[InboxItem], done: list[str]):
            finished = [i for i in pending if i.id in wanted]
            remaining = [i for i in pending if i.id not in wanted]
            return remaining, done + [_to_line(i, done_at=now) for i in finished]

        await self._write(change)


__all__ = [
    "THREAD_UPDATE",
    "Inbox",
    "InboxItem",
    "InboxPost",
    "register_consumer",
    "unregister_all",
]
