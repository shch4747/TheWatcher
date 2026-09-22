"""Scripted stand-ins for the structured ingestion calls (ADR-0012).

`TestModel` always returns the same text whatever the prompt, which
can't tell an assignment call from a thread-update call, so ingestion
tests subclass `TextModelClient` and script `generate_structured`
instead - the same pattern the old skill-dispatching ScriptedWorker
used, adapted to the two JSON contracts.
"""
from __future__ import annotations

import re
from collections.abc import Callable

from agents.wa_agent.assign import (
    Assignment,
    AssignmentResponse,
    ItemOut,
    NewThread,
    ThreadUpdate,
)
from shared.models.schemas import StructuredResult, TextResult
from shared.models.text import TextModelClient

_ID_RE = re.compile(r"^- id=(\S+) \|", re.MULTILINE)


def prompt_message_ids(prompt: str) -> list[str]:
    """The ids listed under 'Messages to assign' in an assignment
    prompt, in order - what a scripted answer has to cover."""
    start = prompt.find("## Messages to assign")
    return _ID_RE.findall(prompt[start:] if start >= 0 else prompt)


class ScriptedStructuredWorker(TextModelClient):
    """`route` maps a message id to a thread ref ("chatter", an existing
    slug, or a "new-N" that gets declared automatically). Anything not
    in `route` falls back to `default_route`. `title`/`summary`/`items`
    script the thread-update call.
    """

    def __init__(
        self,
        route: dict[str, str] | None = None,
        default_route: str = "new-1",
        title: str = "Booking the seminar hall",
        summary: str = "A test thread about booking the seminar hall.",
        items: list[ItemOut] | None = None,
        new_thread_titles: dict[str, str] | None = None,
        text_reply: str | Callable[[str], str] = "",
    ):
        self.model_name = "scripted-structured-worker"
        self.route = route or {}
        self.default_route = default_route
        self.title = title
        self.summary = summary
        self.items = items if items is not None else []
        self.new_thread_titles = new_thread_titles or {}
        self.text_reply = text_reply
        self.assignment_prompts: list[str] = []
        self.update_prompts: list[str] = []

    async def generate(self, prompt: str, system: str | None = None):  # type: ignore[override]
        text = self.text_reply(prompt) if callable(self.text_reply) else self.text_reply
        return TextResult(text=text, input_tokens=10, output_tokens=10)

    async def generate_structured(self, prompt, output_type, system=None):  # type: ignore[override]
        if output_type is AssignmentResponse:
            self.assignment_prompts.append(prompt)
            output = self._assign(prompt)
        elif output_type is ThreadUpdate:
            self.update_prompts.append(prompt)
            output = ThreadUpdate(title=self.title, summary=self.summary, items=list(self.items))
        else:  # pragma: no cover - a test asked for a contract nothing scripts
            raise AssertionError(f"unscripted output_type: {output_type!r}")
        return StructuredResult(output=output, input_tokens=10, output_tokens=10)

    def _assign(self, prompt: str) -> AssignmentResponse:
        assignments = []
        new_ids: list[str] = []
        for message_id in prompt_message_ids(prompt):
            ref = self.route.get(message_id, self.default_route)
            assignments.append(Assignment(message_id=message_id, thread=ref))
            if ref.startswith("new-") and ref not in new_ids and f"### {ref} " not in prompt:
                new_ids.append(ref)
        return AssignmentResponse(
            new_threads=[
                NewThread(
                    id=ref,
                    title=self.new_thread_titles.get(ref, self.title),
                    description="A new thread minted by the scripted worker.",
                )
                for ref in new_ids
            ],
            assignments=assignments,
        )
