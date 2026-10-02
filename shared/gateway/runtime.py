"""Hook and Gowa-client lifecycle for the Gateway. One runtime owns the
HTTP client and the lists of message/reaction/health hooks so tests can
construct an isolated instance instead of mutating process globals.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select

from shared.db import MessageBuffer, OutboundLog, get_session
from shared.gateway.buffer import InboundMessage
from shared.gateway.events import wrap_backfilled_message
from shared.gateway.gowa_client import GowaClient
from shared.gateway.types import SendResult

logger = logging.getLogger(__name__)

MessageHook = Callable[[InboundMessage], Awaitable[None]]
ReactionHook = Callable[[str, str | None, str | None], Awaitable[object]]
HealthLine = Callable[[], Awaitable[str]]


class HookRegistry:
    """Message, reaction, and /health contributors registered at startup."""

    def __init__(self) -> None:
        self.message_hooks: list[MessageHook] = []
        self.reaction_hooks: list[ReactionHook] = []
        self.health_lines: list[HealthLine] = []

    def register_message_hook(self, hook: MessageHook) -> None:
        self.message_hooks.append(hook)

    def register_reaction_hook(self, hook: ReactionHook) -> None:
        self.reaction_hooks.append(hook)

    def register_health_line(self, line: HealthLine) -> None:
        self.health_lines.append(line)

    def clear(self) -> None:
        self.message_hooks.clear()
        self.reaction_hooks.clear()
        self.health_lines.clear()


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

    async def check_gowa_connection(self) -> dict:
        return await self.client.session_status()

    async def bot_jid(self) -> str | None:
        return await self.client.login_jid()

    async def request_history(self, channel: str, count: int) -> int:
        """Backfill: ask gowa for stored history and buffer whatever we don't
        already have, flagged `event_type="message.backfill"`."""
        messages = await self.client.get_chat_messages(channel, limit=count)
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

    async def group_name_map(self) -> dict[str, str]:
        """jid -> WhatsApp group display name, best-effort. Empty on any gowa error."""
        try:
            groups = await self.client.list_groups()
            return {g["JID"]: g["Name"] for g in groups if g.get("JID") and g.get("Name")}
        except Exception:  # noqa: BLE001 - best-effort lookup, never breaks /channels
            return {}
