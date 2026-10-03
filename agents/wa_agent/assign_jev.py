"""Jev assignment (optional; `ASSIGNMENT_MODE=jev`).

The structured assigner in `assign.py` stays. This path asks OpenRouter's
Jev Latest which existing thread each message continues. A message Jev
calls a new thread, or one it is not confident about, is handed to the
Worker (GLM) — Jev returns a label, not a title — which groups those
messages and names the threads. Replies still never reach a model.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from shared.config import settings
from shared.models.interface import ChoiceResult, TextModelClient
from shared.observability.interface import CLASSIFICATION, scope

from agents.wa_agent.assign import (
    _RECENCY_HINT,
    CHATTER,
    NEW_THREAD,
    AssignmentResult,
    SenderNames,
    ThreadInfo,
    _Placement,
    assign_batch,
    by_last_message,
    format_message_line,
)
from agents.wa_agent.messages import BufferedMessage

logger = logging.getLogger(__name__)


class ThreadDecider(Protocol):
    async def choice_batch(
        self,
        state: str,
        questions: dict[str, tuple[str, dict[str, str | None]]],
    ) -> dict[str, ChoiceResult]: ...


def _criteria(threads: list[ThreadInfo]) -> dict[str, str | None]:
    options: dict[str, str | None] = {
        thread.slug: f"{thread.title}. {thread.summary}".strip()
        for thread in by_last_message(threads)
    }
    options[NEW_THREAD] = "Starts a topic none of the existing threads cover."
    options[CHATTER] = "Content-free small talk: greetings, thanks, emoji-only."
    return options


def _state(
    channel_title: str,
    threads: list[ThreadInfo],
    messages: list[BufferedMessage],
    names: SenderNames,
) -> str:
    ordered = by_last_message(threads)
    thread_lines = "\n".join(f"- {thread.slug}: {thread.title}" for thread in ordered) or "(none)"
    message_lines = "\n".join(format_message_line(message, names) for message in messages)
    return (
        f"Channel: {channel_title}\n\n"
        f"Existing threads:\n{thread_lines}\n"
        f"{_RECENCY_HINT}\n\n"
        f"Messages:\n{message_lines}"
    )


def _needs_worker(choice: ChoiceResult | None, known: set[str], threshold: float) -> bool:
    if choice is None:
        return True
    if choice.option == NEW_THREAD or choice.option not in known | {CHATTER}:
        return True
    return choice.confidence < threshold


def _merge(into: AssignmentResult, extra: AssignmentResult) -> None:
    for key, bucket in extra.buckets.items():
        into.buckets.setdefault(key, []).extend(bucket)
    into.new_threads.update(extra.new_threads)
    into.model_calls += extra.model_calls
    into.unassigned += extra.unassigned


async def assign_with_jev(
    messages: list[BufferedMessage],
    threads: list[ThreadInfo],
    small_model: TextModelClient,
    names: SenderNames,
    channel_title: str,
    channel_kind: str,
    initiative: str | None,
    find_thread_for_reply: Callable[[str], Awaitable[ThreadInfo | None]],
    decider: ThreadDecider,
) -> AssignmentResult:
    """Jev labels each unrouted message. New-thread and low-confidence
    messages go to `small_model` through the structured assigner."""
    placement = _Placement(messages)
    await placement.route_replies(find_thread_for_reply)
    if not placement.to_ask:
        return placement.finish()

    known = {thread.slug for thread in threads}
    criteria = _criteria(threads)
    questions = {
        message.message_id: (
            f"Which thread does message id={message.message_id} belong to? "
            "Pick exactly one option.",
            criteria,
        )
        for message in placement.to_ask
    }
    try:
        with scope(phase=CLASSIFICATION):
            choices = await decider.choice_batch(
                _state(channel_title, threads, placement.to_ask, names), questions
            )
        placement.result.model_calls += 1
    except Exception:
        logger.exception("Jev assignment failed; the worker will assign this chunk")
        choices = {}

    threshold = settings.decision_confidence_threshold
    held: list[BufferedMessage] = []
    for message in placement.to_ask:
        choice = choices.get(message.message_id)
        if _needs_worker(choice, known, threshold):
            held.append(message)
            continue
        assert choice is not None
        placement.put(message, CHATTER if choice.option == CHATTER else choice.option)

    if held:
        handed: list[BufferedMessage] = []
        seen: set[str] = set()
        for message in held:
            group = [message]
            for src_id in message.src_ids:
                group.extend(placement.followers.pop(src_id, []))
            for item in group:
                if item.message_id not in seen:
                    seen.add(item.message_id)
                    handed.append(item)
        handed.sort(key=lambda item: placement.order[item.message_id])
        created = await assign_batch(
            handed,
            threads,
            small_model,
            names,
            channel_title,
            channel_kind,
            initiative,
            find_thread_for_reply,
        )
        _merge(placement.result, created)

    return placement.finish()
