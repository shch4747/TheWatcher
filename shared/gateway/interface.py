"""Gateway interface (ADR-0003): the only entry/exit point for WhatsApp.

Implementation lives in package-private modules; this file is the facade
callers and tests import. The default `runtime` owns the gowa client and
hook lists for the process.
"""
from __future__ import annotations

from shared.gateway.agent_switch import chat_agent_enabled as chat_agent_enabled
from shared.gateway.buffer import InboundMessage as InboundMessage
from shared.gateway.buffer import get_context as get_context
from shared.gateway.buffer import get_message as get_message
from shared.gateway.buffer import get_messages as get_messages
from shared.gateway.buffer import mark_consumed as mark_consumed
from shared.gateway.buffer import pending_messages as pending_messages
from shared.gateway.buffer import recent_messages as recent_messages
from shared.gateway.channels import (
    advance_channel_cursor,
    get_channel,
    get_channel_by_kind,
    is_bot_admin,
    is_bot_outbound,
    link_member,
    list_channels,
    resolve_sender,
)
from shared.gateway.command_handlers import (
    channels_report as _channels_report,
)
from shared.gateway.command_handlers import (
    handle_command as _handle_command,
)
from shared.gateway.command_handlers import (
    health as _health,
)
from shared.gateway.command_handlers import (
    link,
    status,
    trigger_ingest as _trigger_ingest,
)
from shared.gateway.onboarding import SETUP_STEPS, continue_setup_session, setup, unwatch
from shared.gateway.proposals import ProposalRecord, ProposalStore
from shared.gateway.runtime import (
    ChannelIngestHook,
    ChannelWatchedHook,
    GatewayRuntime,
    HealthLine,
    HookRegistry,
    MessageHook,
    ReactionHook,
)
from shared.gateway.types import ChannelInfo, MemberLink, SendResult
from shared.gateway.undo_hook import handle_undo_reaction as handle_undo_reaction
from shared.gateway.webhook import receive_webhook as _receive_webhook
from shared.gateway.webhook import verify_signature
from shared.wiki.interface import VaultClient

runtime = GatewayRuntime()
_client = runtime.client
_message_hooks = runtime.hooks.message_hooks
_reaction_hooks = runtime.hooks.reaction_hooks
_health_lines = runtime.hooks.health_lines


def register_message_hook(hook: MessageHook) -> None:
    runtime.hooks.register_message_hook(hook)


def register_reaction_hook(hook: ReactionHook) -> None:
    runtime.hooks.register_reaction_hook(hook)


def register_channel_watched_hook(hook: ChannelWatchedHook) -> None:
    """Called after `/setup` has watched a channel and read its history."""
    runtime.hooks.register_channel_watched_hook(hook)


def register_channel_ingest_hook(hook: ChannelIngestHook) -> None:
    """Register the composition-root handler for an on-demand channel ingest."""
    runtime.hooks.register_channel_ingest_hook(hook)


def register_health_line(line: HealthLine) -> None:
    runtime.hooks.register_health_line(line)


def clear_hooks() -> None:
    runtime.hooks.clear()


async def receive_webhook(raw_body: bytes, signature: str | None) -> bool:
    return await _receive_webhook(raw_body, signature, runtime)


async def send(channel: str, text: str, reply_to: str | None = None) -> SendResult:
    return await runtime.send(channel, text, reply_to=reply_to)


async def check_gowa_connection() -> dict:
    return await runtime.check_gowa_connection()


async def bot_jid() -> str | None:
    """This session's WhatsApp JID (`4591712054@s.whatsapp.net`), or None."""
    return await runtime.bot_jid()


async def handle_command(
    channel_jid: str,
    sender: str,
    text: str,
    vault: VaultClient | None = None,
    mentions: list[str] | None = None,
) -> str | None:
    return await _handle_command(channel_jid, sender, text, runtime, vault=vault, mentions=mentions)


async def health() -> str:
    return await _health(runtime)


async def channels_report(requested_by: str) -> str:
    return await _channels_report(requested_by, runtime)


async def trigger_ingest(requested_by: str, channel_jid: str | None = None) -> str:
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /ingest."
    if channel_jid is None:
        return "Run /ingest in the channel you want to process."
    return await _trigger_ingest(requested_by, channel_jid, runtime)


async def notify_logs(text: str) -> bool:
    """Post `text` to the logs channel if one is set up."""
    logs = await get_channel_by_kind("logs")
    if logs is None:
        return False
    await send(logs.jid, text)
    return True


async def request_history(channel: str, count: int) -> int:
    return await runtime.request_history(channel, count)


async def react(message_id: str, channel: str, emoji: str) -> None:
    await runtime.react(message_id, channel, emoji)


__all__ = [
    "ChannelInfo",
    "MemberLink",
    "ProposalRecord",
    "ProposalStore",
    "SendResult",
    "InboundMessage",
    "HookRegistry",
    "GatewayRuntime",
    "runtime",
    "register_message_hook",
    "register_reaction_hook",
    "handle_undo_reaction",
    "register_channel_watched_hook",
    "register_channel_ingest_hook",
    "ChannelWatchedHook",
    "register_health_line",
    "clear_hooks",
    "verify_signature",
    "chat_agent_enabled",
    "is_bot_admin",
    "is_bot_outbound",
    "get_channel",
    "get_channel_by_kind",
    "list_channels",
    "advance_channel_cursor",
    "receive_webhook",
    "send",
    "check_gowa_connection",
    "bot_jid",
    "setup",
    "continue_setup_session",
    "unwatch",
    "status",
    "channels_report",
    "trigger_ingest",
    "health",
    "link",
    "link_member",
    "handle_command",
    "resolve_sender",
    "notify_logs",
    "request_history",
    "react",
    "get_context",
    "get_message",
    "get_messages",
    "recent_messages",
    "mark_consumed",
    "pending_messages",
    "SETUP_STEPS",
]
