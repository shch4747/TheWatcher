"""Chat Agent: mention/reply-to-bot -> identify thread -> read or propose."""
from __future__ import annotations

import re

from shared.config import settings
from shared.gateway.interface import InboundMessage, get_channel, is_bot_outbound
from shared.gateway.interface import send as gateway_send
from shared.models.interface import DecisionModelProtocol, TextModelClient, decide_with_fallback, generate
from shared.observability.interface import CHAT, scope
from shared.wiki.interface import ThreadStore, VaultClient

from agents.wa_agent.ingestion import thread_info
from agents.wa_agent.messages import ThreadInfo
from agents.wa_agent.proposals import propose_wiki_write

_MENTION_RE = re.compile(rf"@{re.escape(settings.bot_mention_name)}\b", re.IGNORECASE)
_WRITE_VERBS_RE = re.compile(r"\b(note|record|add|mark|remember)\b", re.IGNORECASE)


def is_bot_mention(text: str) -> bool:
    return bool(_MENTION_RE.search(text))


def is_write_request(text: str) -> bool:
    return bool(_WRITE_VERBS_RE.search(text))


async def is_reply_to_bot(quoted_message_id: str | None) -> bool:
    if not quoted_message_id:
        return False
    return await is_bot_outbound(quoted_message_id)


def thread_context(thread: ThreadInfo) -> str:
    parts = [thread.title]
    if thread.summary:
        parts.append(f"Summary: {thread.summary}")
    if thread.recent_context:
        parts.append(f"Recent messages: {thread.recent_context}")
    return " | ".join(parts)


class ChatAgent:
    def __init__(
        self,
        vault: VaultClient,
        decision_client: DecisionModelProtocol,
        worker_client: TextModelClient,
    ) -> None:
        self.vault = vault
        self.decision_client = decision_client
        self.worker_client = worker_client

    async def handle(self, message: InboundMessage) -> str | None:
        if not await self._is_addressed(message):
            return None
        channel = await get_channel(message.channel)
        if channel is None:
            return None
        store = ThreadStore(self.vault, channel)
        thread = await self._resolve_thread(message.text, message.replied_to_id, store)
        if thread is None:
            reply = "I don't see an active thread to answer that from yet."
            await gateway_send(message.channel, reply, reply_to=message.message_id)
            return reply
        if is_write_request(message.text):
            return await self._propose_write(message.channel, thread, message.text)
        return await self._answer(message, thread)

    async def _is_addressed(self, message: InboundMessage) -> bool:
        return is_bot_mention(message.text) or await is_reply_to_bot(message.replied_to_id)

    async def _resolve_thread(
        self,
        text: str,
        quoted_message_id: str | None,
        store: ThreadStore,
    ) -> ThreadInfo | None:
        if quoted_message_id:
            thread = await store.find_by_src_id(quoted_message_id)
            if thread is not None:
                return thread_info(thread)

        active_threads = [thread_info(t) for t in await store.threads()]
        if not active_threads:
            return None
        if len(active_threads) == 1:
            return active_threads[0]

        options: dict[str, str | None] = {t.slug: thread_context(t) for t in active_threads}
        question = f"Which active thread is this chat message about?\n\nMessage: {text}"
        with scope(phase=CHAT):
            result = await decide_with_fallback(
                self.decision_client, self.worker_client, question, options
            )
        return next((t for t in active_threads if t.slug == result.option), active_threads[0])

    async def _answer(self, message: InboundMessage, thread: ThreadInfo) -> str:
        with scope(phase=CHAT):
            result = await generate(
                self.worker_client,
                "worker",
                (
                    f"Thread summary:\n{thread.summary}\n\nQuestion: {message.text}\n\n"
                    "Answer using only the summary above."
                ),
            )
        answer = result.text.strip()
        await gateway_send(message.channel, answer, reply_to=message.message_id)
        return answer

    async def _propose_write(self, channel_jid: str, thread: ThreadInfo, text: str) -> str:
        return await propose_wiki_write(channel_jid, thread.path, "Notes", f"- {text}")


async def identify_thread(
    text: str,
    quoted_message_id: str | None,
    store: ThreadStore,
    decision_client: DecisionModelProtocol,
    worker_client: TextModelClient,
) -> ThreadInfo | None:
    agent = ChatAgent(store.vault, decision_client, worker_client)
    return await agent._resolve_thread(text, quoted_message_id, store)


async def answer_from_wiki(thread: ThreadInfo, question: str, worker_client: TextModelClient) -> str:
    prompt = (
        f"Thread summary:\n{thread.summary}\n\nQuestion: {question}\n\n"
        "Answer using only the summary above."
    )
    result = await generate(worker_client, "worker", prompt)
    return result.text.strip()


async def interpret_text_approval(reply_text: str, decision_client: DecisionModelProtocol) -> bool:
    result = await decision_client.noul(f'Is this an approval? "{reply_text}"')
    return result.answer


async def handle_chat_message(
    message: InboundMessage,
    vault: VaultClient,
    decision_client: DecisionModelProtocol,
    worker_client: TextModelClient,
) -> str | None:
    return await ChatAgent(vault, decision_client, worker_client).handle(message)
