"""WA Agent interface (ingestion + chat agent, Spec: "WhatsApp Agent").
This module implements batch cutting, thread assignment and thread page
writing (Plan Phase 4; assignment redesigned in ADR-0012 - one structured
Worker call per chunk of messages, no Decision Model) and the Chat Agent
(mention/reply, Proposal writes). Nothing outside this package imports
below this module (ADR-0003); this module itself only calls the other
packages through *their* interfaces.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from shared.config import settings
from shared.db import Channel, MessageBuffer, Notice, OutboundLog, aware_utc, get_session
from shared.gateway.interface import (
    get_channel_by_kind,
    list_channels,
    notify_logs,
    parse_gowa_event,
    parse_gowa_timestamp,
    propose_wiki_write,
    resolve_sender,
)
from shared.gateway.interface import send as gateway_send
from shared.models.decision import DecisionModelProtocol
from shared.models.interface import decide_with_fallback, generate
from shared.models.text import TextModelClient
from shared.wiki.interface import (
    VaultClient,
    append_lines,
    dump_page,
    parse_page,
    read_if_exists,
    render_new_thread_page,
    set_fenced,
    set_field,
    set_title,
    slugify,
)
from sqlalchemy import select

from agents.wa_agent.assign import (
    CHATTER,
    NEW_THREAD,
    SenderNames,
    assign_batch,
    update_thread,
)
from agents.wa_agent.messages import BufferedMessage, ThreadInfo

__all__ = [
    "BufferedMessage",
    "ThreadInfo",
    "BatchResult",
    "run_batch",
    "cut_batch",
    "is_batch_ready",
    "list_active_threads",
    "regenerate_channel_threads_index",
    "check_and_mark_stale",
    "archive_ended_threads",
    "run_lifecycle_for_channel",
    "sunday_stale_nudge",
    "handle_chat_message",
    "identify_thread",
    "find_thread_by_src_id",
]


# Channel kinds that are never ingested into threads. `logs` is the
# bot's own output channel (`/setup logs`, see notify_logs).
NON_INGESTED_KINDS = frozenset({"logs"})


def _extract_message(payload: dict) -> dict:
    event = parse_gowa_event(payload)
    return {
        "id": event.message_id,
        "text": event.text,
        "sender": event.sender or "unknown",
        "sender_name": event.sender_name,
        "sent_at": parse_gowa_timestamp(event.timestamp),
        "replied_to_id": event.replied_to_id,
    }


@dataclass
class BatchResult:
    channel: str
    message_count: int
    threads_updated: list[str] = field(default_factory=list)
    threads_created: list[str] = field(default_factory=list)
    threads_revived: list[str] = field(default_factory=list)
    chatter_count: int = 0
    model_calls: int = 0
    unassigned: int = 0


def is_batch_ready(messages: list[BufferedMessage], now: datetime | None = None) -> bool:
    """N or T triggers a cut, then a quiet period with no new message must
    have elapsed (Spec: "wait for a 5-min quiet period")."""
    if not messages:
        return False
    now = now or datetime.now(UTC)
    first = messages[0].received_at
    last = messages[-1].received_at

    size_or_time_triggered = len(messages) >= settings.batch_n or (
        now - first >= timedelta(minutes=settings.batch_t_minutes)
    )
    quiet_elapsed = now - last >= timedelta(minutes=settings.batch_quiet_minutes)
    return size_or_time_triggered and quiet_elapsed


async def _unprocessed_messages(channel: str) -> list[BufferedMessage]:
    async with get_session() as session:
        rows = await session.scalars(
            select(MessageBuffer)
            .where(MessageBuffer.channel == channel, MessageBuffer.processed.is_(False))
            .where(MessageBuffer.event_type.in_(("message", "message.backfill")))
            .order_by(MessageBuffer.id)
        )
    out = []
    for r in rows:
        fields = _extract_message(json.loads(r.payload))
        received_at = aware_utc(r.received_at)
        assert received_at is not None  # MessageBuffer.received_at always has a default
        out.append(
            BufferedMessage(
                row_id=r.id,
                message_id=r.message_id,
                channel=r.channel,
                received_at=received_at,
                text=fields["text"],
                sender=fields["sender"],
                sender_name=fields["sender_name"],
                sent_at=fields["sent_at"],
                replied_to_id=fields["replied_to_id"],
            )
        )
    return out


async def cut_batch(
    channel: str, now: datetime | None = None, force: bool = False
) -> list[BufferedMessage] | None:
    """Idempotent on message ids: callers mark rows processed after a
    successful write, so a retry sees the same unprocessed set again.
    `force=True` (the `/ingest` command) skips the N/T/quiet thresholds
    entirely and cuts whatever's unprocessed right now - for "why hasn't
    this shown up yet" debugging, not something the scheduled tick ever
    sets."""
    messages = await _unprocessed_messages(channel)
    if not messages:
        return None
    if not force and not is_batch_ready(messages, now):
        return None
    return messages[: settings.batch_n] if len(messages) > settings.batch_n else messages


def _concatenate_same_sender(messages: list[BufferedMessage]) -> list[BufferedMessage]:
    """Same-sender plain (non-reply) messages within
    MESSAGE_CONCAT_WINDOW_MINUTES of the immediately preceding message
    fold into one logical message before assignment - a quick burst of
    follow-ups ("wait", "actually let's do Friday") reads as one
    message, not several that can each land in a different thread. A
    message that IS a reply never folds into a preceding run (it gets
    its own reply-based routing); the rolling gap is measured against
    the true previous raw message, not the group's start time, so a
    long burst of quick messages doesn't get cut off just because it
    collectively spans more than the window."""
    if not messages:
        return []
    window = timedelta(minutes=settings.message_concat_window_minutes)
    merged: list[BufferedMessage] = []
    last_raw_time: datetime | None = None
    for m in messages:
        if (
            merged
            and m.sender == merged[-1].sender
            and m.replied_to_id is None
            and last_raw_time is not None
            and m.received_at - last_raw_time <= window
        ):
            prev = merged[-1]
            merged[-1] = replace(
                prev, text=f"{prev.text}\n{m.text}", src_ids=[*prev.src_ids, *m.src_ids]
            )
        else:
            merged.append(m)
        last_raw_time = m.received_at
    return merged


def _recent_timeline_excerpt(timeline_body: str) -> str:
    """Last few Timeline lines, capped by character budget (not line
    count) - the thread page doesn't keep raw messages separately, so
    this is the cheapest way to give the assignment prompt a "recent
    messages" excerpt without re-reading `messages_buffer`."""
    lines = [
        line.strip()
        for line in timeline_body.splitlines()
        if line.strip().startswith("- ") and "<!--" not in line
    ]
    budget = settings.thread_context_max_chars
    picked: list[str] = []
    total = 0
    for line in reversed(lines):
        total += len(line) + 1
        picked.append(line)
        if total >= budget:
            break
    picked.reverse()
    return "\n".join(picked)


async def list_active_threads(vault: VaultClient, channel_dir: str) -> list[ThreadInfo]:
    """Active AND stale threads - stale ones must still be assignable so
    a new message can revive them (Spec: "stale ... revivable")."""
    paths = await vault.list(f"channels/{channel_dir}")
    threads = []
    for path in paths:
        if "/archive/" in path or path.count("/") < 2:
            continue  # channel index page itself, not a thread
        result = await vault.read(path)
        page = parse_page(result.content)
        state = getattr(page.frontmatter, "state", None)
        if page.page_type != "thread" or state not in ("active", "stale"):
            continue
        summary_section = page.section("Summary")
        timeline_section = page.section("Timeline")
        threads.append(
            ThreadInfo(
                slug=page.frontmatter.slug,
                path=path,
                title=page.frontmatter.title,
                recent_context=_recent_timeline_excerpt(
                    timeline_section.body if timeline_section else ""
                ),
                summary=(summary_section.body if summary_section else "").strip(),
                state=state,
            )
        )
    return threads


async def _sender_names(messages: list[BufferedMessage]) -> SenderNames:
    """One registry lookup per distinct sender in the batch."""
    names = SenderNames()
    for m in messages:
        if m.sender not in names.members:
            registry = await resolve_sender(m.sender)
            names.members[m.sender] = registry.member_title if registry else None
    return names


def _timeline_line(message: BufferedMessage, names: SenderNames) -> str:
    """`- <sent time> — <who>: <text> [src:: ids]` - who is the member
    wikilink when the sender is linked, else their display name, never a
    bare number when gowa gave us a name. A reply also carries
    `[reply_to:: id]` so the link survives on the page."""
    ts = message.when.strftime("%Y-%m-%d %H:%M")
    src = ", ".join(message.src_ids)
    reply = f" [reply_to:: {message.replied_to_id}]" if message.replied_to_id else ""
    text = " ".join(part.strip() for part in message.text.splitlines() if part.strip())
    return f"- {ts} — {names.wiki_name(message)}: {text} [src:: {src}]{reply}"


def _participants(messages: list[BufferedMessage], names: SenderNames) -> list[str]:
    """Frontmatter `participants:` - `[[Member]]` for linked senders,
    the display name otherwise (the template quotes each entry)."""
    seen: list[str] = []
    for m in messages:
        who = names.wiki_name(m)
        if who not in seen:
            seen.append(who)
    return sorted(seen)


_TITLE_MAX_WORDS = 5
_TITLE_MAX_CHARS = 60  # safety net for pathologically long "words" (no spaces)
_SUMMARY_MAX_CHARS = 800


_META_COMMENTARY_RE = re.compile(
    r"\b(transcript|inert data|third-party|third party|no substantive content|"
    r"no action required|not sure what|as an ai|i notice|understood[,.]?$|"
    r"as requested|per (the|your) instructions)\b",
    re.IGNORECASE,
)


def _looks_like_meta_commentary(title: str) -> bool:
    """Even with explicit "don't acknowledge this framing" instructions,
    a Worker model can still slip into describing its own task instead
    of doing it - "Understood, the transcript has been received and
    treated as inert data" is a real observed title, not a hypothetical.
    Length/single-line sanitizing alone doesn't catch this since it's
    well-formed prose; this is a second, content-level check."""
    return bool(_META_COMMENTARY_RE.search(title))


_TITLE_LEADING_MARKER_RE = re.compile(r"^[\-*•]+\s*|^\d+[.)]\s*")
# A leading label like "Title:", "**Message summary:**", "Summary -" -
# real observed output was a whole "**Message summary:** An incoming
# message..." sentence, not a title.
_TITLE_LEADING_LABEL_RE = re.compile(r"^[*_]{0,2}[A-Za-z][A-Za-z \-]{0,30}[*_]{0,2}:\*{0,2}\s*-?\s*")
_CODE_FENCE_RE = re.compile(r"^```\w*\s*$", re.MULTILINE)


def _strip_title_markers(text: str) -> str:
    """Strip a leading list/bullet marker ("- ", "* ", "1. "), a leading
    label ("Title:", "**Summary:**"), and any stray markdown emphasis/
    code-fence characters - a title is plain text, never formatted, no
    matter how the model dressed it up."""
    text = _CODE_FENCE_RE.sub("", text).strip()
    text = _TITLE_LEADING_MARKER_RE.sub("", text)
    text = _TITLE_LEADING_LABEL_RE.sub("", text)
    text = re.sub(r"[`*_]", "", text)
    return text.strip().strip("\"'")


def _sanitize_title(raw: str, fallback_text: str = "") -> str:
    """A title is frontmatter metadata and a slug source, not free
    prose - collapse to one line, strip wrapping quotes/markdown, and
    cap it to `_TITLE_MAX_WORDS` words, with a char-count safety net for
    pathological single "words". This is a content-quality guard
    (catches a merely long-winded but well-behaved title);
    `shared.wiki.templates.yaml_str` is the separate structural guard
    that keeps any title, sanitized or not, from corrupting the
    frontmatter block. If the result still reads as the model commenting
    on its own task rather than doing it, fall back to the raw first
    message's own words instead - always more useful than a
    meta-description, however trivial."""

    def _first_line(text: str) -> str:
        cleaned = _strip_title_markers(text)
        line = cleaned.splitlines()[0] if cleaned.strip() else ""
        return re.sub(r"\s+", " ", line).strip()

    first_line = _first_line(raw)
    if not first_line or _looks_like_meta_commentary(first_line):
        first_line = _first_line(fallback_text)

    words = first_line.split(" ")
    if len(words) > _TITLE_MAX_WORDS:
        first_line = " ".join(words[:_TITLE_MAX_WORDS]).rstrip(",.;:- ")
    if len(first_line) > _TITLE_MAX_CHARS:
        head = first_line[:_TITLE_MAX_CHARS]
        first_line = (head.rsplit(" ", 1)[0] if " " in head else head).rstrip(",.;:- ")

    return first_line or "Untitled thread"


def _sanitize_summary(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw.strip())
    if _looks_like_meta_commentary(text):
        return ""  # meta-commentary is worse than an empty Summary section
    if len(text) > _SUMMARY_MAX_CHARS:
        head = text[:_SUMMARY_MAX_CHARS]
        text = (head.rsplit(". ", 1)[0] + "." if ". " in head else head).rstrip()
    return text


async def _mark_processed(rows: list[BufferedMessage]) -> None:
    async with get_session() as session:
        for m in rows:
            row = await session.get(MessageBuffer, m.row_id)
            if row is not None:
                row.processed = True
        await session.commit()


async def _advance_cursor(channel_jid: str, last_message_id: str) -> None:
    async with get_session() as session:
        channel = await session.scalar(select(Channel).where(Channel.jid == channel_jid))
        if channel is not None:
            channel.cursor = last_message_id
            await session.commit()


async def _post_notice(channel: Channel, thread_slug: str, since_message_id: str | None) -> None:
    if channel.kind not in ("project", "event") or not channel.initiative:
        return
    async with get_session() as session:
        session.add(
            Notice(
                agent="project_agent",
                channel=channel.jid,
                thread_slug=thread_slug,
                since_message_id=since_message_id,
            )
        )
        await session.commit()


async def run_batch(
    channel_jid: str,
    vault: VaultClient,
    worker_client: TextModelClient,
    now: datetime | None = None,
    force: bool = False,
    assign_client: TextModelClient | None = None,
) -> BatchResult | None:
    """Orchestrates one batch: cut -> assign (structured, ADR-0012) ->
    one update call + page write per touched thread -> advance cursor
    -> mark processed -> post notices. Returns None if the batch isn't
    ready yet (Spec: batch cut per channel, idempotent). `force=True`
    bypasses the N/T/quiet-period thresholds in cut_batch - see its
    docstring. `assign_client` (INGEST_MODEL_NAME) lets the assignment
    and thread-update calls run on a different model than the Worker."""
    messages = await cut_batch(channel_jid, now, force=force)
    if messages is None:
        return None

    async with get_session() as session:
        channel = await session.scalar(select(Channel).where(Channel.jid == channel_jid))
    if channel is None:
        return None

    if channel.kind in NON_INGESTED_KINDS:
        # The logs channel is the bot talking to itself (job failures,
        # ingest warnings) - threading and summarising that is noise at
        # best and a feedback loop at worst. Drain the buffer so the
        # rows don't pile up unprocessed forever, and write nothing.
        await _mark_processed(messages)
        await _advance_cursor(channel_jid, messages[-1].message_id)
        return BatchResult(channel=channel_jid, message_count=len(messages))

    model = assign_client or worker_client
    channel_dir = slugify(channel.title or channel_jid)
    channel_title = channel.title or channel_jid
    active_threads = await list_active_threads(vault, channel_dir)
    merged_messages = _concatenate_same_sender(messages)
    names = await _sender_names(merged_messages)

    async def _find_for_reply(src_id: str) -> ThreadInfo | None:
        return await find_thread_by_src_id(vault, channel_dir, src_id, threads=active_threads)

    assignment = await assign_batch(
        merged_messages,
        active_threads,
        model,
        names,
        channel_title,
        channel.kind,
        channel.initiative,
        _find_for_reply,
    )
    if assignment.unassigned:
        await notify_logs(
            f"⚠️ ingest: {assignment.unassigned} message(s) in {channel_title} could not be "
            "assigned to a thread after retries and were dropped as chatter."
        )

    result = BatchResult(
        channel=channel_jid,
        message_count=len(messages),
        model_calls=assignment.model_calls,
        unassigned=assignment.unassigned,
    )
    threads_by_slug = {t.slug: t for t in active_threads}
    used_slugs = {t.slug for t in active_threads}
    touched_channel = False

    for key, bucket_messages in assignment.buckets.items():
        if key == CHATTER:
            result.chatter_count += len(bucket_messages)
            continue

        timeline_lines = [_timeline_line(m, names) for m in bucket_messages]
        last_message_at = bucket_messages[-1].when.isoformat()
        batch_src_ids = {src_id for m in bucket_messages for src_id in m.src_ids}

        if key.startswith(f"{NEW_THREAD}:"):
            minted = assignment.new_threads[key]
            update, items_text = await update_thread(
                model, minted.title, minted.description, "", bucket_messages, names, batch_src_ids
            )
            result.model_calls += 1
            title = _sanitize_title(update.title or minted.title, fallback_text=bucket_messages[0].text)
            base_slug = f"{bucket_messages[0].when.strftime('%Y%m%d')}-{slugify(title)}"
            slug = base_slug
            suffix = 2
            while slug in used_slugs:
                # Two distinct new threads in the same batch landing on
                # the same date+title slug is real, not hypothetical. A
                # write colliding here isn't a stale-revision conflict
                # our normal ConflictError handling catches - it's a
                # genuine duplicate path, so disambiguate before writing.
                slug = f"{base_slug}-{suffix}"
                suffix += 1
            used_slugs.add(slug)
            page_text = render_new_thread_page(
                slug=slug,
                title=title,
                channel_title=channel_title,
                initiative=channel.initiative,
                summary=_sanitize_summary(update.summary) or minted.description.strip(),
                items_text=items_text,
                timeline_lines=timeline_lines,
                participants=_participants(bucket_messages, names),
                message_ids=[src_id for m in bucket_messages for src_id in m.src_ids],
                opened_at=bucket_messages[0].when,
                last_message_at=bucket_messages[-1].when,
            )
            await vault.create(f"channels/{channel_dir}/{slug}.md", page_text)
            result.threads_created.append(slug)
            await _post_notice(channel, slug, bucket_messages[-1].message_id)
            touched_channel = True
        else:
            thread = threads_by_slug[key]
            existing = await vault.read(thread.path)
            page = parse_page(existing.content)
            items_section = page.section("Items")
            known_ids = set(getattr(page.frontmatter, "message_ids", []) or [])
            update, items_text = await update_thread(
                model,
                thread.title,
                thread.summary,
                items_section.body if items_section else "",
                bucket_messages,
                names,
                batch_src_ids | known_ids,
            )
            result.model_calls += 1
            title = _sanitize_title(update.title, fallback_text=thread.title)
            if title != thread.title:
                page = set_title(page, title)
            summary = _sanitize_summary(update.summary) or thread.summary
            page = set_fenced(page, "Summary", summary)
            page = set_fenced(page, "Items", items_text)
            page = append_lines(page, "Timeline", timeline_lines)
            page = set_field(page, "last_message_at", last_message_at)
            if thread.state == "stale":
                # a new message revives a stale thread (Spec: "revivable") -
                # last_message_at above is what keeps it revived: without
                # bumping it, the next lifecycle_tick would just see the
                # same stale timestamp and mark it stale again immediately.
                page = set_field(page, "state", "active")
                result.threads_revived.append(key)
            await vault.write(thread.path, dump_page(page), base_revision=existing.revision)
            result.threads_updated.append(key)
            await _post_notice(channel, key, bucket_messages[-1].message_id)
            touched_channel = True

    if touched_channel:
        await regenerate_channel_threads_index(vault, channel, channel_dir)

    await _mark_processed(messages)
    await _advance_cursor(channel_jid, messages[-1].message_id)
    return result


def _lapis_link(path: str) -> str:
    return f"{settings.lapis_base_url}/vault/{settings.lapis_vault_id}/file/{path}"


def _index_line(path: str, title: str, last_message_at: datetime | None) -> str:
    """Wiki Format §channel: `- [[channels/<dir>/<slug>|Title]] — last
    <date>` - a wikilink (rename-safe, resolves in Obsidian and Lapis),
    not a markdown link to a Lapis URL."""
    target = path.removesuffix(".md")
    line = f"- [[{target}|{title}]]"
    if last_message_at is not None:
        line += f" — last {last_message_at.strftime('%Y-%m-%d')}"
    return line


async def regenerate_channel_threads_index(vault: VaultClient, channel: Channel, channel_dir: str) -> None:
    """Rewrite the channel's own page's Active/Stale/Archived threads
    sections from what's actually in the vault right now. Called after
    every thread create/update/stale/revive/archive so a Bot Admin
    browsing the channel page in Lapis sees current links immediately,
    not on some later, unrelated schedule. A channel with no page yet
    (nothing has backfilled it via `/setup <kind>`) is a no-op, not an
    error - there's nothing to write into."""
    page_path = f"channels/{slugify(channel.title or channel.jid)}.md"
    existing = await read_if_exists(vault, page_path)
    if existing is None:
        return
    page = parse_page(existing.content)
    if page.page_type != "channel":
        return

    active_lines: list[str] = []
    stale_lines: list[str] = []
    for path in await vault.list(f"channels/{channel_dir}"):
        if "/archive/" in path or path.count("/") < 2:
            continue
        result = await vault.read(path)
        thread_page = parse_page(result.content)
        if thread_page.page_type != "thread":
            continue
        line = _index_line(
            path, thread_page.frontmatter.title, getattr(thread_page.frontmatter, "last_message_at", None)
        )
        state = getattr(thread_page.frontmatter, "state", None)
        if state == "active":
            active_lines.append(line)
        elif state == "stale":
            stale_lines.append(line)

    archived_lines: list[str] = []
    for path in await vault.list(f"channels/archive/{channel_dir}"):
        if path.count("/") < 3:
            continue
        result = await vault.read(path)
        thread_page = parse_page(result.content)
        if thread_page.page_type != "thread":
            continue
        archived_lines.append(
            _index_line(
                path, thread_page.frontmatter.title, getattr(thread_page.frontmatter, "last_message_at", None)
            )
        )

    page = set_fenced(page, "Active threads", "\n".join(active_lines))
    page = set_fenced(page, "Stale threads", "\n".join(stale_lines))
    page = set_fenced(page, "Archived threads", "\n".join(archived_lines))
    await vault.write(page_path, dump_page(page), base_revision=existing.revision)


async def check_and_mark_stale(
    vault: VaultClient, channel_dir: str, now: datetime | None = None
) -> list[str]:
    """`active` -> `stale` after `thread_stale_days` without a message
    (Spec: thread states). Ended threads are handled separately - only a
    human sets `state: ended`, this job never does."""
    now = now or datetime.now(UTC)
    marked = []
    for path in await vault.list(f"channels/{channel_dir}"):
        if "/archive/" in path or path.count("/") < 2:
            continue
        result = await vault.read(path)
        page = parse_page(result.content)
        if page.page_type != "thread" or getattr(page.frontmatter, "state", None) != "active":
            continue
        last_message_at = getattr(page.frontmatter, "last_message_at", None)
        if last_message_at is None:
            continue
        if last_message_at.tzinfo is None:
            last_message_at = last_message_at.replace(tzinfo=UTC)
        if now - last_message_at >= timedelta(days=settings.thread_stale_days):
            updated = set_field(page, "state", "stale")
            await vault.write(path, dump_page(updated), base_revision=result.revision)
            marked.append(page.frontmatter.slug)
    return marked


async def archive_ended_threads(vault: VaultClient, channel_dir: str) -> list[str]:
    """A human setting `state: ended` in the wiki moves the file to
    `channels/archive/<channel>/` on the next run (Spec)."""
    archived = []
    for path in await vault.list(f"channels/{channel_dir}"):
        if "/archive/" in path or path.count("/") < 2:
            continue
        result = await vault.read(path)
        page = parse_page(result.content)
        if page.page_type != "thread" or getattr(page.frontmatter, "state", None) != "ended":
            continue
        archive_path = f"channels/archive/{channel_dir}/{path.split('/')[-1]}"
        await vault.create(archive_path, result.content)
        await vault.delete(path)
        archived.append(page.frontmatter.slug)
    return archived


async def run_lifecycle_for_channel(vault: VaultClient, channel_jid: str) -> dict[str, list[str]]:
    """One channel's worth of staleness + archival - a Scheduler job body
    wires this per channel (Phase 3's scheduler, daily)."""
    async with get_session() as session:
        channel = await session.scalar(select(Channel).where(Channel.jid == channel_jid))
    if channel is None:
        return {"stale": [], "archived": []}
    channel_dir = slugify(channel.title or channel_jid)
    stale = await check_and_mark_stale(vault, channel_dir)
    archived = await archive_ended_threads(vault, channel_dir)
    if stale or archived:
        await regenerate_channel_threads_index(vault, channel, channel_dir)
    return {"stale": stale, "archived": archived}


async def sunday_stale_nudge(vault: VaultClient, now: datetime | None = None) -> str | None:
    """Every Sunday: counts of stale threads and those silent 7+ days,
    with up to 10 Lapis links, posted to the coordis channel (Spec)."""
    now = now or datetime.now(UTC)
    coordis = await get_channel_by_kind("coordis")
    if coordis is None:
        return None

    stale_paths: list[str] = []
    silent_paths: list[str] = []
    for channel in await list_channels():
        channel_dir = slugify(channel.title or channel.jid)
        for path in await vault.list(f"channels/{channel_dir}"):
            if "/archive/" in path or path.count("/") < 2:
                continue
            result = await vault.read(path)
            page = parse_page(result.content)
            if page.page_type != "thread":
                continue
            state = getattr(page.frontmatter, "state", None)
            last_message_at = getattr(page.frontmatter, "last_message_at", None)
            if state == "stale":
                stale_paths.append(path)
            if last_message_at is not None:
                if last_message_at.tzinfo is None:
                    last_message_at = last_message_at.replace(tzinfo=UTC)
                if now - last_message_at >= timedelta(days=settings.thread_silent_days):
                    silent_paths.append(path)

    links = "\n".join(f"- {_lapis_link(p)}" for p in stale_paths[:10])
    text = (
        f"There are {len(stale_paths)} stale threads, {len(silent_paths)} of them with no "
        f"messages in the last {int(settings.thread_silent_days)} days — please mark ended ones."
    )
    if links:
        text += f"\n{links}"

    await gateway_send(coordis.jid, text)
    return text


_MENTION_RE = re.compile(rf"@{re.escape(settings.bot_mention_name)}\b", re.IGNORECASE)
_WRITE_VERBS_RE = re.compile(r"\b(note|record|add|mark|remember)\b", re.IGNORECASE)


def is_bot_mention(text: str) -> bool:
    return bool(_MENTION_RE.search(text))


async def is_reply_to_bot(quoted_message_id: str | None) -> bool:
    if not quoted_message_id:
        return False
    async with get_session() as session:
        row = await session.scalar(
            select(OutboundLog).where(OutboundLog.message_id == quoted_message_id)
        )
    return row is not None


def is_write_request(text: str) -> bool:
    """Deterministic heuristic distinguishing "note that X" from a
    question - Spec doesn't mandate a specific mechanism, only that
    every write goes through a Proposal regardless of how it's detected."""
    return bool(_WRITE_VERBS_RE.search(text))


async def find_thread_by_src_id(
    vault: VaultClient, channel_dir: str, src_id: str, threads: list[ThreadInfo] | None = None
) -> ThreadInfo | None:
    """A plain substring check, not an exact `[src:: id]` match - a
    Timeline line can carry more than one id (`[src:: id1, id2]`, from
    same-sender message concatenation), and a reply can target any of
    them, not just the first. `threads` lets a caller that already
    listed the channel's threads skip re-listing them."""
    for thread in threads if threads is not None else await list_active_threads(vault, channel_dir):
        result = await vault.read(thread.path)
        if src_id in result.content:
            return thread
    return None


def _thread_context(thread: ThreadInfo) -> str:
    """The description a Chat Agent Choice option gets for this thread -
    title, Summary, and a recent-messages excerpt."""
    parts = [thread.title]
    if thread.summary:
        parts.append(f"Summary: {thread.summary}")
    if thread.recent_context:
        parts.append(f"Recent messages: {thread.recent_context}")
    return " | ".join(parts)


async def identify_thread(
    text: str,
    quoted_message_id: str | None,
    vault: VaultClient,
    channel_dir: str,
    decision_client: DecisionModelProtocol,
    worker_client: TextModelClient,
) -> ThreadInfo | None:
    """The quoted message's thread if there is one, else the Decision
    Model picks among active threads (Spec: Chat Agent flow)."""
    if quoted_message_id:
        thread = await find_thread_by_src_id(vault, channel_dir, quoted_message_id)
        if thread is not None:
            return thread

    active_threads = await list_active_threads(vault, channel_dir)
    if not active_threads:
        return None
    if len(active_threads) == 1:
        return active_threads[0]

    options: dict[str, str | None] = {t.slug: _thread_context(t) for t in active_threads}
    question = f"Which active thread is this chat message about?\n\nMessage: {text}"
    result = await decide_with_fallback(decision_client, worker_client, question, options)
    return next((t for t in active_threads if t.slug == result.option), active_threads[0])


async def answer_from_wiki(thread: ThreadInfo, question: str, worker_client: TextModelClient) -> str:
    """Read-only QA (Spec: Chat Agent, non-write path) - answers strictly
    from the thread's own Summary, nothing outside the wiki."""
    prompt = (
        f"Thread summary:\n{thread.summary}\n\nQuestion: {question}\n\n"
        "Answer using only the summary above."
    )
    result = await generate(worker_client, "worker", prompt)
    return result.text.strip()


async def interpret_text_approval(reply_text: str, decision_client: DecisionModelProtocol) -> bool:
    """A text reply (not a reaction) to a Proposal is interpreted by a
    Decision Model Noul: "is this an approval?" (Spec: Chat Agent)."""
    result = await decision_client.noul(f"Is this an approval? \"{reply_text}\"")
    return result.answer


async def handle_chat_message(
    payload: dict,
    channel_jid: str,
    vault: VaultClient,
    decision_client: DecisionModelProtocol,
    worker_client: TextModelClient,
) -> str | None:
    """Deterministic trigger (mention or reply-to-bot) -> identify thread
    -> read-only answer or Proposal for a write. Returns the reply text
    sent, or None if the message wasn't addressed to the bot."""
    fields = _extract_message(payload)
    text = fields["text"]
    message_id = fields["id"]
    quoted_id = fields["replied_to_id"]

    mentioned = is_bot_mention(text)
    replying_to_bot = await is_reply_to_bot(quoted_id)
    if not mentioned and not replying_to_bot:
        return None

    async with get_session() as session:
        channel = await session.scalar(select(Channel).where(Channel.jid == channel_jid))
    if channel is None:
        return None
    channel_dir = slugify(channel.title or channel_jid)

    thread = await identify_thread(text, quoted_id, vault, channel_dir, decision_client, worker_client)
    if thread is None:
        reply = "I don't see an active thread to answer that from yet."
        await gateway_send(channel_jid, reply, reply_to=message_id)
        return reply

    if is_write_request(text):
        reply = await propose_wiki_write(channel_jid, thread.path, "Notes", f"- {text}")
        return reply

    answer = await answer_from_wiki(thread, text, worker_client)
    await gateway_send(channel_jid, answer, reply_to=message_id)
    return answer
