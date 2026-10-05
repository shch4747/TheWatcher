"""Hook and Gowa-client lifecycle for the Gateway. One runtime owns the
HTTP client and the lists of message/reaction/health hooks so tests can
construct an isolated instance instead of mutating process globals.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select

from shared.config import settings
from shared.db import MessageBuffer, OutboundLog, get_session
from shared.gateway.buffer import InboundMessage
from shared.gateway.events import parse_gowa_timestamp, wrap_backfilled_message
from shared.gateway.gowa_client import GowaClient
from shared.gateway.types import ChannelInfo, GroupParticipant, SendResult

logger = logging.getLogger(__name__)

MessageHook = Callable[[InboundMessage], Awaitable[None]]
ReactionHook = Callable[[str, str | None, str | None], Awaitable[object]]
HealthLine = Callable[[], Awaitable[str]]
ChannelIngestHook = Callable[[str], Awaitable[str]]
# Fired once a channel has been set up and its history imported, with the
# channel as registered. The composition root uses it to start an ingest.
ChannelWatchedHook = Callable[[ChannelInfo], Awaitable[None]]

# gowa's `/chat/{jid}/messages` never returns more than this per call.
HISTORY_PAGE_SIZE = 100
# Kinds whose messages are never ingested, so reading history is wasted work.
HISTORY_SKIPPED_KINDS = frozenset({"logs"})
_EPOCH = datetime.fromtimestamp(0, UTC)


class HookRegistry:
    """Message, reaction, channel-watched, and /health contributors
    registered at startup."""

    def __init__(self) -> None:
        self.message_hooks: list[MessageHook] = []
        self.reaction_hooks: list[ReactionHook] = []
        self.channel_watched_hooks: list[ChannelWatchedHook] = []
        self.health_lines: list[HealthLine] = []
        self.channel_ingest_hooks: list[ChannelIngestHook] = []

    def register_message_hook(self, hook: MessageHook) -> None:
        self.message_hooks.append(hook)

    def register_reaction_hook(self, hook: ReactionHook) -> None:
        self.reaction_hooks.append(hook)

    def register_channel_watched_hook(self, hook: ChannelWatchedHook) -> None:
        self.channel_watched_hooks.append(hook)

    def register_health_line(self, line: HealthLine) -> None:
        self.health_lines.append(line)

    def register_channel_ingest_hook(self, hook: ChannelIngestHook) -> None:
        self.channel_ingest_hooks.append(hook)

    def clear(self) -> None:
        self.message_hooks.clear()
        self.reaction_hooks.clear()
        self.channel_watched_hooks.clear()
        self.health_lines.clear()
        self.channel_ingest_hooks.clear()


class GatewayRuntime:
    """Owns the gowa HTTP client and hook lists for one process."""

    def __init__(self, client: GowaClient | None = None, hooks: HookRegistry | None = None) -> None:
        self.client = client or GowaClient()
        self.hooks = hooks or HookRegistry()

    async def send(self, channel: str, text: str, reply_to: str | None = None) -> SendResult:
        """Send a message and log it. Never merges/batches sends (Spec: Gateway)."""
        result = await self.client.send_text(channel, text, reply_message_id=reply_to)
        message_id = result.get("results", {}).get("message_id") or result.get("message_id")

        async with get_session() as session:
            session.add(OutboundLog(channel=channel, text=text, reply_to=reply_to, message_id=message_id))
            await session.commit()

        return SendResult(message_id=message_id)

    async def react(self, message_id: str, channel: str, emoji: str) -> None:
        """Bot-sent reaction. Not logged to outbound_log since it isn't a text send."""
        await self.client.react(message_id, channel, emoji)

    async def ingest_channel(self, channel: str) -> str:
        if not self.hooks.channel_ingest_hooks:
            return "On-demand ingestion isn't registered - is the app running via main.py?"
        return await self.hooks.channel_ingest_hooks[-1](channel)

    async def check_gowa_connection(self) -> dict:
        return await self.client.session_status()

    async def bot_jid(self) -> str | None:
        return await self.client.login_jid()

    async def _fetch_history(self, channel: str, count: int) -> list[dict]:
        """Up to `count` of the newest stored messages, paging through gowa."""
        fetched: list[dict] = []
        while len(fetched) < count:
            wanted = min(HISTORY_PAGE_SIZE, count - len(fetched))
            page = await self.client.get_chat_messages(channel, limit=wanted, offset=len(fetched))
            fetched.extend(page)
            if len(page) < wanted:
                break
        return fetched

    async def request_history(self, channel: str, count: int) -> int:
        """Backfill: ask gowa for stored history and buffer whatever we don't
        already have, flagged `event_type="message.backfill"`. Buffered
        oldest-first, because ingestion reads the buffer in arrival order
        and gowa lists newest-first."""
        messages = sorted(
            await self._fetch_history(channel, count),
            key=lambda m: parse_gowa_timestamp(m.get("timestamp")) or _EPOCH,
        )
        added = 0
        for m in messages:
            wrapped = wrap_backfilled_message(m)
            message_id = wrapped["payload"]["id"]
            if not message_id:
                continue
            async with get_session() as session:
                existing = await session.scalar(
                    select(MessageBuffer).where(MessageBuffer.message_id == message_id)
                )
                if existing is not None:
                    continue
                session.add(
                    MessageBuffer(
                        message_id=message_id,
                        channel=channel,
                        event_type="message.backfill",
                        payload=json.dumps(wrapped),
                    )
                )
                await session.commit()
                added += 1
        return added

    async def on_channel_watched(self, channel: ChannelInfo) -> str | None:
        """Seed a freshly set-up channel: read its stored history and tell
        the channel-watched hooks there is something to ingest. Best-effort
        - a gowa failure must never undo the `/setup`. Returns a line for
        the `/setup` reply, or None when there is nothing to say."""
        if channel.kind in HISTORY_SKIPPED_KINDS:
            return None
        try:
            added = await self.request_history(channel.jid, settings.setup_history_count)
        except Exception as exc:  # noqa: BLE001 - the admin needs the reason, not a traceback
            logger.warning("could not read history for %s: %s", channel.jid, exc)
            return f"Couldn't read the chat history ({exc}); only new messages will be ingested."
        if added == 0:
            return "No earlier chat history to read."
        for hook in self.hooks.channel_watched_hooks:
            try:
                await hook(channel)
            except Exception:  # noqa: BLE001 - a broken hook must not break /setup
                logger.exception("channel-watched hook %r failed for %s", hook, channel.jid)
        return f"Read {added} message(s) from the chat history; ingesting them now."

    async def group_participants(self, group_id: str) -> list[GroupParticipant]:
        """Everyone in the WhatsApp group. Raises if gowa cannot list them."""
        people: list[GroupParticipant] = []
        for item in await self.client.group_participants(group_id):
            if not isinstance(item, dict):
                continue
            jid = str(item.get("jid") or "").strip()
            if not jid:
                continue
            name = item.get("display_name")
            lid = item.get("lid")
            phone = item.get("phone_number")
            people.append(
                GroupParticipant(
                    jid=jid,
                    display_name=str(name).strip() if name else None,
                    lid=str(lid).strip() if lid else None,
                    phone_number=str(phone).strip() if phone else None,
                )
            )
        return people

    async def group_name_map(self) -> dict[str, str]:
        """jid -> WhatsApp group display name, best-effort. Empty on any gowa error."""
        try:
            groups = await self.client.list_groups()
            return {g["JID"]: g["Name"] for g in groups if g.get("JID") and g.get("Name")}
        except Exception:  # noqa: BLE001 - best-effort lookup, never breaks /channels
            return {}
