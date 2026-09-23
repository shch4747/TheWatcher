"""WA Agent interface (ingestion + chat agent, Spec: "WhatsApp Agent").
This module implements batch cutting, thread assignment and thread page
writing (Plan Phase 4; assignment redesigned in ADR-0012 - one structured
Worker call per chunk of messages, no Decision Model) and the Chat Agent
(mention/reply, Proposal writes). Nothing outside this package imports
below this module (ADR-0003); this module itself only calls the other
packages through *their* interfaces.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from shared.config import settings
from shared.db import Channel, OutboundLog, get_session
from shared.gateway.interface import (
    InboundMessage,
    get_channel,
    get_channel_by_kind,
    list_channels,
    mark_consumed,
    notify_logs,
    pending_messages,
    propose_wiki_write,
    resolve_sender,
)
from shared.gateway.interface import send as gateway_send
from shared.inbox.interface import THREAD_UPDATE, Inbox, InboxPost
from shared.models.decision import DecisionModelProtocol
from shared.models.interface import decide_with_fallback, generate
from shared.models.text import TextModelClient
from shared.wiki.interface import StoredThread, ThreadChange, ThreadDraft, ThreadStore, VaultClient
from sqlalchemy import select

from agents.wa_agent.assign import CHATTER, NEW_THREAD, SenderNames, assign_batch
from agents.wa_agent.messages import BufferedMessage, ThreadInfo
from agents.wa_agent.revise import ThreadRevision, revise_thread

logger = logging.getLogger(__name__)

__all__ = [
    "BufferedMessage",
    "ThreadInfo",
    "BatchResult",
    "run_batch",
    "cut_batch",
    "is_batch_ready",
    "run_lifecycle_for_channel",
    "sunday_stale_nudge",
    "handle_chat_message",
    "identify_thread",
    "ThreadRevision",
    "revise_thread",
]


# Channel kinds that are never ingested into threads. `logs` is the
# bot's own output channel (`/setup logs`, see notify_logs).
NON_INGESTED_KINDS = frozenset({"logs"})


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
    # min/max, not first/last: an edit moves an earlier message's
    # received_at forward, and that activity restarts the quiet period too
    first = min(m.received_at for m in messages)
    last = max(m.received_at for m in messages)

    size_or_time_triggered = len(messages) >= settings.batch_n or (
        now - first >= timedelta(minutes=settings.batch_t_minutes)
    )
    quiet_elapsed = now - last >= timedelta(minutes=settings.batch_quiet_minutes)
    return size_or_time_triggered and quiet_elapsed


def _buffered(message: InboundMessage) -> BufferedMessage:
    return BufferedMessage(
        row_id=message.row_id,
        message_id=message.message_id,
        channel=message.channel,
        received_at=message.received_at,
        text=message.text,
        sender=message.sender,
        sender_name=message.sender_name,
        sent_at=message.sent_at,
        replied_to_id=message.replied_to_id,
    )


async def cut_batch(
    channel: str, now: datetime | None = None, force: bool = False
) -> list[BufferedMessage] | None:
    """Idempotent on message ids: callers mark rows processed after a
    successful write, so a retry sees the same unprocessed set again.
    `force=True` (the `/ingest` command) skips the N/T/quiet thresholds
    entirely and cuts whatever's unprocessed right now - for "why hasn't
    this shown up yet" debugging, not something the scheduled tick ever
    sets."""
    messages = [_buffered(m) for m in await pending_messages(channel)]
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


def _recent_timeline_excerpt(timeline: list[str]) -> str:
    """Last few Timeline lines, capped by character budget (not line
    count) - the thread page doesn't keep raw messages separately, so
    this is the cheapest way to give the assignment prompt a "recent
    messages" excerpt without re-reading `messages_buffer`."""
    budget = settings.thread_context_max_chars
    picked: list[str] = []
    total = 0
    for line in reversed(timeline):
        total += len(line) + 1
        picked.append(line)
        if total >= budget:
            break
    picked.reverse()
    return "\n".join(picked)


def _thread_info(thread: StoredThread) -> ThreadInfo:
    """What the assignment prompt and the Chat Agent see of a Thread."""
    return ThreadInfo(
        slug=thread.slug,
        path=thread.path,
        title=thread.title,
        summary=thread.summary.strip(),
        state=thread.state,
        recent_context=_recent_timeline_excerpt(thread.timeline),
    )


async def _sender_names(messages: list[BufferedMessage]) -> SenderNames:
    """One registry lookup per distinct sender in the batch."""
    names = SenderNames()
    for m in messages:
        if m.sender not in names.members:
            registry = await resolve_sender(m.sender)
            names.members[m.sender] = registry.member_title if registry else None
    return names


async def _mark_processed(rows: list[BufferedMessage]) -> None:
    await mark_consumed(m.row_id for m in rows)


async def _advance_cursor(channel_jid: str, last_message_id: str) -> None:
    async with get_session() as session:
        channel = await session.scalar(select(Channel).where(Channel.jid == channel_jid))
        if channel is not None:
            channel.cursor = last_message_id
            await session.commit()


def _inbox_for(channel: Channel) -> str | None:
    """Whose Inbox hears about this Channel's Threads (Spec: the
    initiative's agent - the Project Agent for project/event Channels,
    nobody else in v1)."""
    if channel.kind in ("project", "event") and channel.initiative:
        return "project_agent"
    return None


def _update_notice(channel: Channel, thread: StoredThread, since: str) -> InboxPost:
    return InboxPost(
        kind=THREAD_UPDATE,
        text=f"Thread update: {thread.title}",
        channel=channel.jid,
        thread_slug=thread.slug,
        thread_link=thread.path.removesuffix(".md"),
        since=since,
    )


async def _post_notices(vault: VaultClient, channel: Channel, notices: list[InboxPost]) -> None:
    """After the batch is committed: a notice that can't be posted is
    reported, not raised - raising would re-run a batch whose Thread
    pages were already written."""
    agent = _inbox_for(channel)
    if agent is None or not notices:
        return
    try:
        await Inbox(vault, agent).post(*notices)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.exception("could not post %d update notice(s) to %s", len(notices), agent)
        await notify_logs(f"⚠️ ingest: couldn't post update notices to {agent}'s inbox: {exc}")


async def run_batch(
    channel_jid: str,
    vault: VaultClient,
    worker_client: TextModelClient,
    now: datetime | None = None,
    force: bool = False,
    assign_client: TextModelClient | None = None,
) -> BatchResult | None:
    """One batch: cut -> assign (structured, ADR-0012) -> revise + write
    each touched Thread -> reindex the Channel Page -> advance cursor ->
    mark processed -> post notices. Returns None if the batch isn't
    ready yet (Spec: batch cut per channel, idempotent). `force=True`
    bypasses the N/T/quiet-period thresholds in cut_batch - see its
    docstring. `assign_client` (INGEST_MODEL_NAME) lets the assignment
    and thread-update calls run on a different model than the Worker."""
    messages = await cut_batch(channel_jid, now, force=force)
    if messages is None:
        return None

    channel = await get_channel(channel_jid)
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
    store = ThreadStore(vault, channel)
    open_threads = [_thread_info(t) for t in await store.threads()]
    merged_messages = _concatenate_same_sender(messages)
    names = await _sender_names(merged_messages)

    async def _find_for_reply(src_id: str) -> ThreadInfo | None:
        thread = await store.find_by_src_id(src_id)
        return _thread_info(thread) if thread else None

    channel_title = channel.title or channel_jid
    assignment = await assign_batch(
        merged_messages,
        open_threads,
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

    notices: list[InboxPost] = []
    for key, bucket in assignment.buckets.items():
        if key == CHATTER:
            result.chatter_count += len(bucket)
            continue

        if key.startswith(f"{NEW_THREAD}:"):
            revision = await revise_thread(model, bucket, names, minted=assignment.new_threads[key])
            thread = await store.create(
                ThreadDraft(
                    title=revision.title,
                    summary=revision.summary,
                    items_body=revision.items_body,
                    timeline_lines=revision.timeline_lines,
                    participants=revision.participants,
                    message_ids=revision.message_ids,
                    opened_at=revision.opened_at,
                    last_message_at=revision.last_message_at,
                )
            )
            result.threads_created.append(thread.slug)
        else:
            revision = await revise_thread(model, bucket, names, current=await store.get(key))
            thread, revived = await store.revise(
                key,
                ThreadChange(
                    title=revision.title,
                    summary=revision.summary,
                    items_body=revision.items_body,
                    timeline_lines=revision.timeline_lines,
                    message_ids=revision.message_ids,
                    participants=revision.participants,
                    last_message_at=revision.last_message_at,
                ),
            )
            result.threads_updated.append(thread.slug)
            if revived:
                result.threads_revived.append(thread.slug)
        result.model_calls += 1
        notices.append(_update_notice(channel, thread, bucket[0].message_id))

    if result.threads_created or result.threads_updated:
        await store.reindex()

    await _mark_processed(messages)
    await _advance_cursor(channel_jid, messages[-1].message_id)
    await _post_notices(vault, channel, notices)
    return result


def _lapis_link(path: str) -> str:
    return f"{settings.lapis_base_url}/vault/{settings.lapis_vault_id}/file/{path}"


async def run_lifecycle_for_channel(
    vault: VaultClient, channel: Channel, now: datetime | None = None
) -> dict[str, list[str]]:
    """One Channel's daily lifecycle pass: `active -> stale` after
    THREAD_STALE_DAYS of silence, human-`ended` Threads into the archive,
    and the Channel Page's index regenerated if anything moved."""
    store = ThreadStore(vault, channel)
    stale = await store.mark_stale(now or datetime.now(UTC), timedelta(days=settings.thread_stale_days))
    archived = await store.archive_ended()
    if stale or archived:
        await store.reindex()
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
    silent_after = timedelta(days=settings.thread_silent_days)
    for channel in await list_channels():
        for thread in await ThreadStore(vault, channel).threads(("active", "stale", "ended")):
            if thread.state == "stale":
                stale_paths.append(thread.path)
            if thread.last_message_at is not None and now - thread.last_message_at >= silent_after:
                silent_paths.append(thread.path)

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
    store: ThreadStore,
    decision_client: DecisionModelProtocol,
    worker_client: TextModelClient,
) -> ThreadInfo | None:
    """The quoted message's thread if there is one, else the Decision
    Model picks among active threads (Spec: Chat Agent flow)."""
    if quoted_message_id:
        thread = await store.find_by_src_id(quoted_message_id)
        if thread is not None:
            return _thread_info(thread)

    active_threads = [_thread_info(t) for t in await store.threads()]
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
    message: InboundMessage,
    vault: VaultClient,
    decision_client: DecisionModelProtocol,
    worker_client: TextModelClient,
) -> str | None:
    """Deterministic trigger (mention or reply-to-bot) -> identify thread
    -> read-only answer or Proposal for a write. Returns the reply text
    sent, or None if the message wasn't addressed to the bot."""
    text = message.text
    message_id = message.message_id
    quoted_id = message.replied_to_id
    channel_jid = message.channel

    mentioned = is_bot_mention(text)
    replying_to_bot = await is_reply_to_bot(quoted_id)
    if not mentioned and not replying_to_bot:
        return None

    channel = await get_channel(channel_jid)
    if channel is None:
        return None

    store = ThreadStore(vault, channel)
    thread = await identify_thread(text, quoted_id, store, decision_client, worker_client)
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
