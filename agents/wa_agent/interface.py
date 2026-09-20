"""WA Agent interface (ingestion + chat agent, Spec: "WhatsApp Agent").
This module implements batch cutting, thread assignment and thread page
writing (Plan Phase 4). The Chat Agent (mention/reply, Proposal writes)
is a separate, later ticket. Nothing outside this package imports below
this module (ADR-0003); this module itself only calls the other
packages through *their* interfaces.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from shared.config import settings
from shared.db import Channel, MessageBuffer, Notice, OutboundLog, aware_utc, get_session
from shared.gateway.interface import get_channel_by_kind, list_channels, propose_wiki_write, resolve_sender
from shared.gateway.interface import send as gateway_send
from shared.models.decision import DecisionModelProtocol
from shared.models.interface import decide_with_fallback, generate
from shared.models.skill_loader import load_skills
from shared.models.text import TextModelClient
from shared.wiki.interface import (
    VaultClient,
    append_to_section,
    dump_page,
    parse_page,
    render_new_thread_page,
    replace_managed_section,
    set_frontmatter_field,
    slugify,
)
from sqlalchemy import select

CHATTER = "chatter"
NEW_THREAD = "new-thread"


def _extract_message(payload: dict) -> dict:
    msg = payload.get("message", {})
    return {
        "id": msg.get("id") or payload.get("id"),
        "text": msg.get("text", ""),
        "sender": payload.get("sender") or msg.get("sender") or "unknown",
        "timestamp": msg.get("timestamp") or payload.get("timestamp"),
    }


@dataclass
class BufferedMessage:
    row_id: int
    message_id: str
    channel: str
    received_at: datetime
    text: str
    sender: str


@dataclass
class ThreadInfo:
    slug: str
    path: str
    title: str
    summary: str
    state: str


@dataclass
class BatchResult:
    channel: str
    message_count: int
    threads_updated: list[str] = field(default_factory=list)
    threads_created: list[str] = field(default_factory=list)
    threads_revived: list[str] = field(default_factory=list)
    chatter_count: int = 0


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
            )
        )
    return out


async def cut_batch(channel: str, now: datetime | None = None) -> list[BufferedMessage] | None:
    """Idempotent on message ids: callers mark rows processed after a
    successful write, so a retry sees the same unprocessed set again."""
    messages = await _unprocessed_messages(channel)
    if not is_batch_ready(messages, now):
        return None
    return messages[: settings.batch_n] if len(messages) > settings.batch_n else messages


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
        threads.append(
            ThreadInfo(
                slug=page.frontmatter.slug,
                path=path,
                title=page.frontmatter.title,
                summary=(summary_section.body if summary_section else "").strip(),
                state=state,
            )
        )
    return threads


async def assign_messages_to_threads(
    messages: list[BufferedMessage],
    active_threads: list[ThreadInfo],
    decision_client: DecisionModelProtocol,
    worker_client: TextModelClient,
) -> dict[str, list[BufferedMessage]]:
    """One Decision Model Choice per message over {each active thread,
    new-thread, chatter}, Worker fallback below threshold (Spec). New-
    thread messages are grouped consecutively into one new thread each -
    a practical batch-level heuristic, not a claim that two "new-thread"
    messages far apart in the batch are unrelated.
    """
    options = [t.slug for t in active_threads] + [NEW_THREAD, CHATTER]
    buckets: dict[str, list[BufferedMessage]] = {}
    new_thread_counter = 0
    last_was_new_thread = False

    for message in messages:
        question = f"Which thread does this message belong to?\n\nMessage: {message.text}"
        result = await decide_with_fallback(decision_client, worker_client, question, options)
        choice = result.option

        if choice == CHATTER:
            buckets.setdefault(CHATTER, []).append(message)
            last_was_new_thread = False
        elif choice == NEW_THREAD:
            if not last_was_new_thread:
                new_thread_counter += 1
            key = f"{NEW_THREAD}:{new_thread_counter}"
            buckets.setdefault(key, []).append(message)
            last_was_new_thread = True
        else:
            buckets.setdefault(choice, []).append(message)
            last_was_new_thread = False

    return buckets


def _timeline_line(message: BufferedMessage, member_title: str | None) -> str:
    who = f"[[{member_title}]]" if member_title else message.sender
    ts = message.received_at.strftime("%Y-%m-%d %H:%M")
    return f"- {ts} — {who} {message.text} [src:: {message.message_id}]"


async def _timeline_lines(messages: list[BufferedMessage]) -> list[str]:
    lines = []
    for m in messages:
        registry = await resolve_sender(m.sender)
        lines.append(_timeline_line(m, registry.member_title if registry else None))
    return lines


async def _generate_summary_and_items(
    messages: list[BufferedMessage],
    existing_summary: str,
    worker_client: TextModelClient,
    skills: dict,
) -> tuple[str, str]:
    transcript = "\n".join(f"[{m.message_id}] {m.sender}: {m.text}" for m in messages)

    summary_skill = skills.get("summarise-thread")
    summary_prompt = f"Current summary (may be empty):\n{existing_summary}\n\nNew messages:\n{transcript}"
    summary_result = await generate(
        worker_client, "worker", summary_prompt, system=summary_skill.instructions if summary_skill else None
    )

    items_skill = skills.get("extract-items")
    items_result = await generate(
        worker_client, "worker", transcript, system=items_skill.instructions if items_skill else None
    )
    return summary_result.text.strip(), items_result.text.strip()


async def _mint_title(messages: list[BufferedMessage], worker_client: TextModelClient, skills: dict) -> str:
    naming_skill = skills.get("name-thread")
    transcript = "\n".join(f"{m.sender}: {m.text}" for m in messages)
    result = await generate(
        worker_client, "worker", transcript, system=naming_skill.instructions if naming_skill else None
    )
    return result.text.strip() or "Untitled thread"


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
    decision_client: DecisionModelProtocol,
    worker_client: TextModelClient,
    skills_dir: Path | None = None,
    now: datetime | None = None,
) -> BatchResult | None:
    """Orchestrates one batch: cut -> assign -> write thread pages ->
    advance cursor -> mark processed -> post notices. Returns None if the
    batch isn't ready yet (Spec: batch cut per channel, idempotent)."""

    messages = await cut_batch(channel_jid, now)
    if messages is None:
        return None

    async with get_session() as session:
        channel = await session.scalar(select(Channel).where(Channel.jid == channel_jid))
    if channel is None:
        return None

    channel_dir = slugify(channel.title or channel_jid)
    active_threads = await list_active_threads(vault, channel_dir)
    buckets = await assign_messages_to_threads(messages, active_threads, decision_client, worker_client)

    repo_skills_dir = skills_dir or (Path(__file__).resolve().parents[2] / "skills")
    skills = load_skills(repo_skills_dir)

    result = BatchResult(channel=channel_jid, message_count=len(messages))
    threads_by_slug = {t.slug: t for t in active_threads}

    for key, bucket_messages in buckets.items():
        if key == CHATTER:
            result.chatter_count += len(bucket_messages)
            continue

        timeline_lines = await _timeline_lines(bucket_messages)

        if key.startswith(f"{NEW_THREAD}:"):
            title = await _mint_title(bucket_messages, worker_client, skills)
            slug = f"{bucket_messages[0].received_at.strftime('%Y%m%d')}-{slugify(title)}"
            summary, items_text = await _generate_summary_and_items(
                bucket_messages, "", worker_client, skills
            )
            participants = sorted({m.sender for m in bucket_messages})
            page_text = render_new_thread_page(
                slug=slug,
                title=title,
                channel_title=channel.title or channel_jid,
                initiative=channel.initiative,
                summary=summary,
                items_text=items_text,
                timeline_lines=timeline_lines,
                participants=participants,
                message_ids=[m.message_id for m in bucket_messages],
            )
            await vault.write(f"channels/{channel_dir}/{slug}.md", page_text, base_revision="")
            result.threads_created.append(slug)
            await _post_notice(channel, slug, bucket_messages[-1].message_id)
        else:
            thread = threads_by_slug[key]
            existing = await vault.read(thread.path)
            page = parse_page(existing.content)
            summary, items_text = await _generate_summary_and_items(
                bucket_messages, thread.summary, worker_client, skills
            )
            page = replace_managed_section(page, "Summary", summary)
            page = replace_managed_section(page, "Items", items_text)
            page = append_to_section(page, "Timeline", timeline_lines)
            if thread.state == "stale":
                # a new message revives a stale thread (Spec: "revivable")
                page = set_frontmatter_field(page, "state", "active")
                result.threads_revived.append(key)
            await vault.write(thread.path, dump_page(page), base_revision=existing.revision)
            result.threads_updated.append(key)
            await _post_notice(channel, key, bucket_messages[-1].message_id)

    await _mark_processed(messages)
    await _advance_cursor(channel_jid, messages[-1].message_id)
    return result


def _lapis_link(path: str) -> str:
    return f"{settings.lapis_base_url}/vault/{settings.lapis_vault_id}/file/{path}"


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
            updated = set_frontmatter_field(page, "state", "stale")
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
        await vault.write(archive_path, result.content, base_revision="")
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


async def find_thread_by_src_id(vault: VaultClient, channel_dir: str, src_id: str) -> ThreadInfo | None:
    for thread in await list_active_threads(vault, channel_dir):
        result = await vault.read(thread.path)
        if f"[src:: {src_id}" in result.content or f"[src:: {src_id}]" in result.content:
            return thread
    return None


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

    options = [t.slug for t in active_threads]
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
    quoted_id = payload.get("message", {}).get("replied_to_id")

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
