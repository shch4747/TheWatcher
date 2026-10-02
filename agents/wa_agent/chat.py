"""Chat Agent: @mention the bot -> last 5 messages plus their quoted
replies -> a text answer, with read-only wiki tools if that isn't enough.

No write-proposals. An @mention or a reply to one of the bot's own
messages triggers an answer. A mention or reply that is only media
(sticker/image, no caption) gets a canned "I can't see that" text, not
a model call.
"""
from __future__ import annotations

import re

from shared.config import settings
from shared.gateway.interface import (
    InboundMessage,
    bot_jid,
    get_channel,
    get_message,
    is_bot_outbound,
    recent_messages,
)
from shared.gateway.interface import (
    send as gateway_send,
)
from shared.models.interface import DecisionModelProtocol, TextModelClient, generate_with_tools
from shared.observability.interface import CHAT, scope
from shared.wiki.interface import VaultClient

from agents.wa_agent.wiki_tools import read_only_wiki_tools

CONTEXT_LIMIT = 5
CANNOT_SEE_MEDIA = "I can't see stickers or images. Send a text message."
_SYSTEM = (
    "You are Watcher, a helpful assistant in a WhatsApp group. "
    "Answer from the recent messages when they are enough. Be brief. "
    "If that context is incomplete, look the answer up in the wiki: "
    "start by listing this channel's threads, then search or read the "
    "relevant pages. Do not search when the chat already answers it. "
    "If the question is obviously poking fun or trying to waste tokens, "
    "skip the lookup and reply with one short edgy line that makes fun "
    "of the request. Never write to the wiki."
)

_MENTION_RE = re.compile(rf"@{re.escape(settings.bot_mention_name)}\b", re.IGNORECASE)
_AT_TOKEN_RE = re.compile(r"@([^\s]+)")
_WRITE_VERBS_RE = re.compile(r"\b(note|record|add|mark|remember)\b", re.IGNORECASE)


def _mention_key(token: str) -> str:
    """Local part of a mention: `@Watcher` / `4591712054@s.whatsapp.net` -> `watcher` / `4591712054`."""
    return token.strip().lstrip("@").rstrip(".,!?;:").split("@", 1)[0].lower()


def _bot_aliases(login_jid: str | None = None) -> set[str]:
    names = [settings.bot_mention_name, settings.gowa_device_id, login_jid]
    return {_mention_key(name) for name in names if name}


def is_bot_mention(text: str, login_jid: str | None = None) -> bool:
    aliases = _bot_aliases(login_jid)
    if _MENTION_RE.search(text or ""):
        return True
    return any(_mention_key(token) in aliases for token in _AT_TOKEN_RE.findall(text or ""))


def is_write_request(text: str) -> bool:
    return bool(_WRITE_VERBS_RE.search(text))


async def is_reply_to_bot(quoted_message_id: str | None) -> bool:
    if not quoted_message_id:
        return False
    return await is_bot_outbound(quoted_message_id)


def mentions_bot(message: InboundMessage, login_jid: str | None = None) -> bool:
    aliases = _bot_aliases(login_jid)
    if is_bot_mention(message.text, login_jid):
        return True
    return any(_mention_key(raw) in aliases for raw in message.mentions)


def _is_media_only(message: InboundMessage) -> bool:
    return not (message.text or "").strip() and message.media_kind is not None


def _body(message: InboundMessage) -> str:
    text = (message.text or "").strip()
    if text:
        return text
    if message.media_kind:
        return f"[{message.media_kind}]"
    return "[empty]"


def _format_line(message: InboundMessage, by_id: dict[str, InboundMessage]) -> str:
    name = message.sender_name or message.sender
    body = _body(message)
    if not message.replied_to_id:
        return f"{name}: {body}"
    quoted = by_id.get(message.replied_to_id)
    if quoted is None:
        return f"{name} (reply): {body}"
    qname = quoted.sender_name or quoted.sender
    return f"{name} (replying to {qname}): {body}"


async def _recent_window(message: InboundMessage) -> list[InboundMessage]:
    history = [
        item
        for item in await recent_messages(message.channel, limit=CONTEXT_LIMIT)
        if item.message_id != message.message_id
    ]
    return (history + [message])[-CONTEXT_LIMIT:]


async def _quoted_by(window: list[InboundMessage]) -> list[InboundMessage]:
    """Messages the last-5 window replies to, if they aren't already in it."""
    have = {item.message_id for item in window}
    extras: list[InboundMessage] = []
    for item in window:
        quoted_id = item.replied_to_id
        if not quoted_id or quoted_id in have:
            continue
        quoted = await get_message(quoted_id)
        if quoted is None or quoted.channel != item.channel:
            continue
        extras.append(quoted)
        have.add(quoted_id)
    return extras


async def _context_for(message: InboundMessage) -> list[InboundMessage]:
    window = await _recent_window(message)
    combined = [*await _quoted_by(window), *window]
    seen: set[str] = set()
    ordered: list[InboundMessage] = []
    for item in sorted(combined, key=lambda m: (m.sent_at, m.row_id, m.message_id)):
        if item.message_id in seen:
            continue
        seen.add(item.message_id)
        ordered.append(item)
    return ordered


class ChatAgent:
    def __init__(self, worker_client: TextModelClient, vault: VaultClient) -> None:
        self.worker_client = worker_client
        self.vault = vault

    async def handle(self, message: InboundMessage) -> str | None:
        if message.from_me:
            return None
        addressed = mentions_bot(message, await bot_jid()) or await is_reply_to_bot(
            message.replied_to_id
        )
        if not addressed:
            return None
        if _is_media_only(message):
            await gateway_send(message.channel, CANNOT_SEE_MEDIA, reply_to=message.message_id)
            return CANNOT_SEE_MEDIA
        if not (message.text or "").strip():
            return None
        return await self._answer(message)

    async def _answer(self, message: InboundMessage) -> str:
        context = await _context_for(message)
        by_id = {item.message_id: item for item in context}
        lines = "\n".join(_format_line(item, by_id) for item in context)
        prompt = f"Recent messages:\n{lines}\n\nReply to the latest message."
        channel = await get_channel(message.channel)
        with scope(phase=CHAT):
            result = await generate_with_tools(
                self.worker_client,
                "worker",
                prompt,
                tools=list(read_only_wiki_tools(self.vault, channel)),
                deps=None,
                system=_SYSTEM,
            )
        answer = result.text.strip()
        await gateway_send(message.channel, answer, reply_to=message.message_id)
        return answer


async def interpret_text_approval(reply_text: str, decision_client: DecisionModelProtocol) -> bool:
    result = await decision_client.noul(f'Is this an approval? "{reply_text}"')
    return result.answer


async def handle_chat_message(
    message: InboundMessage, worker_client: TextModelClient, vault: VaultClient
) -> str | None:
    return await ChatAgent(worker_client, vault).handle(message)
