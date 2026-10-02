"""Batch ingestion: cut a channel's pending messages, assign them to
threads, revise the pages, consume the buffer. Scheduled vs forced cuts
are separate entry points that share one processor.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from shared.config import settings
from shared.gateway.interface import (
    ChannelInfo,
    InboundMessage,
    advance_channel_cursor,
    get_channel,
    mark_consumed,
    notify_logs,
    pending_messages,
    resolve_sender,
)
from shared.inbox.interface import THREAD_UPDATE, Inbox, InboxPost
from shared.models.interface import TextModelClient
from shared.wiki.interface import StoredThread, ThreadChange, ThreadDraft, ThreadStore, VaultClient

from agents.wa_agent.assign import CHATTER, NEW_THREAD, SenderNames, assign_batch
from agents.wa_agent.messages import BufferedMessage, ThreadInfo
from agents.wa_agent.revise import revise_thread

logger = logging.getLogger(__name__)

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


def _cap(messages: list[BufferedMessage]) -> list[BufferedMessage]:
    return messages[: settings.batch_n] if len(messages) > settings.batch_n else messages


async def _pending(channel: str) -> list[BufferedMessage]:
    return [_buffered(m) for m in await pending_messages(channel)]


async def cut_scheduled_batch(channel: str, now: datetime | None = None) -> list[BufferedMessage] | None:
    messages = await _pending(channel)
    if not messages or not is_batch_ready(messages, now):
        return None
    return _cap(messages)


async def cut_forced_batch(channel: str) -> list[BufferedMessage] | None:
    messages = await _pending(channel)
    if not messages:
        return None
    return _cap(messages)


async def cut_batch(
    channel: str, now: datetime | None = None, force: bool = False
) -> list[BufferedMessage] | None:
    """Compatibility wrapper. Prefer `cut_scheduled_batch` / `cut_forced_batch`."""
    if force:
        return await cut_forced_batch(channel)
    return await cut_scheduled_batch(channel, now)


def concatenate_same_sender(messages: list[BufferedMessage]) -> list[BufferedMessage]:
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


def recent_timeline_excerpt(timeline: list[str]) -> str:
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


def thread_info(thread: StoredThread) -> ThreadInfo:
    return ThreadInfo(
        slug=thread.slug,
        path=thread.path,
        title=thread.title,
        summary=thread.summary.strip(),
        state=thread.state,
        recent_context=recent_timeline_excerpt(thread.timeline),
    )


async def sender_names(messages: list[BufferedMessage]) -> SenderNames:
    names = SenderNames()
    for m in messages:
        if m.sender not in names.members:
            registry = await resolve_sender(m.sender)
            names.members[m.sender] = registry.member_title if registry else None
    return names


def inbox_for(channel: ChannelInfo) -> str | None:
    if channel.kind in ("project", "event") and channel.initiative:
        return "project_agent"
    return None


def update_notice(channel: ChannelInfo, thread: StoredThread, since: str) -> InboxPost:
    return InboxPost(
        kind=THREAD_UPDATE,
        text=f"Thread update: {thread.title}",
        channel=channel.jid,
        thread_slug=thread.slug,
        thread_link=thread.path.removesuffix(".md"),
        since=since,
    )


async def post_notices(vault: VaultClient, channel: ChannelInfo, notices: list[InboxPost]) -> None:
    agent = inbox_for(channel)
    if agent is None or not notices:
        return
    try:
        await Inbox(vault, agent).post(*notices)
    except Exception as exc:  # noqa: BLE001 - see original docstring
        logger.exception("could not post %d update notice(s) to %s", len(notices), agent)
        await notify_logs(f"⚠️ ingest: couldn't post update notices to {agent}'s inbox: {exc}")


class IngestionPipeline:
    def __init__(
        self,
        vault: VaultClient,
        worker_client: TextModelClient,
        assign_client: TextModelClient | None = None,
    ) -> None:
        self.vault = vault
        self.worker_client = worker_client
        self.assign_client = assign_client or worker_client

    async def run_scheduled(
        self, channel_jid: str, now: datetime | None = None
    ) -> BatchResult | None:
        messages = await cut_scheduled_batch(channel_jid, now)
        return await self._run(channel_jid, messages)

    async def run_forced(self, channel_jid: str) -> BatchResult | None:
        messages = await cut_forced_batch(channel_jid)
        return await self._run(channel_jid, messages)

    async def drain_non_ingested(
        self, channel_jid: str, messages: list[BufferedMessage]
    ) -> BatchResult:
        await mark_consumed(m.row_id for m in messages)
        await advance_channel_cursor(channel_jid, messages[-1].message_id)
        return BatchResult(channel=channel_jid, message_count=len(messages))

    async def _run(self, channel_jid: str, messages: list[BufferedMessage] | None) -> BatchResult | None:
        if messages is None:
            return None
        channel = await get_channel(channel_jid)
        if channel is None:
            return None
        if channel.kind in NON_INGESTED_KINDS:
            return await self.drain_non_ingested(channel_jid, messages)
        return await self._process_batch(channel, messages)

    async def _process_batch(
        self, channel: ChannelInfo, messages: list[BufferedMessage]
    ) -> BatchResult:
        store = ThreadStore(self.vault, channel)
        open_threads = [thread_info(t) for t in await store.threads()]
        merged_messages = concatenate_same_sender(messages)
        names = await sender_names(merged_messages)

        async def _find_for_reply(src_id: str) -> ThreadInfo | None:
            thread = await store.find_by_src_id(src_id)
            return thread_info(thread) if thread else None

        channel_title = channel.title or channel.jid
        assignment = await assign_batch(
            merged_messages,
            open_threads,
            self.assign_client,
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
            channel=channel.jid,
            message_count=len(messages),
            model_calls=assignment.model_calls,
            unassigned=assignment.unassigned,
        )
        notices: list[InboxPost] = []
        for key, bucket in assignment.buckets.items():
            if key == CHATTER:
                result.chatter_count += len(bucket)
                continue
            thread, created, revived = await self._write_bucket(
                store, names, key, bucket, assignment.new_threads
            )
            result.model_calls += 1
            notices.append(update_notice(channel, thread, bucket[0].message_id))
            if created:
                result.threads_created.append(thread.slug)
            else:
                result.threads_updated.append(thread.slug)
                if revived:
                    result.threads_revived.append(thread.slug)

        if result.threads_created or result.threads_updated:
            await store.reindex()

        await mark_consumed(m.row_id for m in messages)
        await advance_channel_cursor(channel.jid, messages[-1].message_id)
        await post_notices(self.vault, channel, notices)
        return result

    async def _write_bucket(
        self, store, names, key, bucket, new_threads
    ) -> tuple[StoredThread, bool, bool]:
        if key.startswith(f"{NEW_THREAD}:"):
            revision = await revise_thread(self.assign_client, bucket, names, minted=new_threads[key])
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
            return thread, True, False
        revision = await revise_thread(self.assign_client, bucket, names, current=await store.get(key))
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
        return thread, False, revived


async def run_batch(
    channel_jid: str,
    vault: VaultClient,
    worker_client: TextModelClient,
    now: datetime | None = None,
    force: bool = False,
    assign_client: TextModelClient | None = None,
) -> BatchResult | None:
    """Compatibility wrapper. Prefer `IngestionPipeline.run_scheduled` / `run_forced`."""
    pipeline = IngestionPipeline(vault, worker_client, assign_client)
    if force:
        return await pipeline.run_forced(channel_jid)
    return await pipeline.run_scheduled(channel_jid, now)
