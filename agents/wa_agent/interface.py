"""WA Agent interface (ingestion + chat agent, Spec: "WhatsApp Agent").

This module is the package's only public surface (ADR-0003). Implementation
lives in package-private modules; callers keep using these names.
"""
from __future__ import annotations

from agents.wa_agent.chat import (
    ChatAgent,
    answer_from_wiki,
    handle_chat_message,
    identify_thread,
    interpret_text_approval,
    is_bot_mention,
    is_reply_to_bot,
    is_write_request,
)
from agents.wa_agent.ingestion import (
    BatchResult,
    IngestionPipeline,
    cut_batch,
    is_batch_ready,
    run_batch,
)
from agents.wa_agent.ingestion import (
    concatenate_same_sender as _concatenate_same_sender,
)
from agents.wa_agent.lifecycle import run_lifecycle_for_channel, sunday_stale_nudge
from agents.wa_agent.messages import BufferedMessage, ThreadInfo
from agents.wa_agent.proposals import (
    ProposalService,
    confirm_proposal,
    expire_stale_proposals,
    handle_reaction,
    pending_count,
    propose_identity_link,
    propose_wiki_write,
)
from agents.wa_agent.revise import ThreadRevision, revise_thread


async def pending_proposals_line() -> str:
    """The `/health` line for Proposals (registered with the Gateway)."""
    return f"pending proposals: {await pending_count()}"


__all__ = [
    "BufferedMessage",
    "ThreadInfo",
    "BatchResult",
    "IngestionPipeline",
    "ChatAgent",
    "ProposalService",
    "run_batch",
    "cut_batch",
    "is_batch_ready",
    "_concatenate_same_sender",
    "run_lifecycle_for_channel",
    "sunday_stale_nudge",
    "handle_chat_message",
    "identify_thread",
    "answer_from_wiki",
    "interpret_text_approval",
    "is_bot_mention",
    "is_reply_to_bot",
    "is_write_request",
    "ThreadRevision",
    "revise_thread",
    "confirm_proposal",
    "expire_stale_proposals",
    "handle_reaction",
    "pending_proposals_line",
    "propose_identity_link",
    "propose_wiki_write",
]
