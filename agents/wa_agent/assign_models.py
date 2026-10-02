"""JSON contracts and small result types for structured assignment."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from agents.wa_agent.messages import BufferedMessage

CHATTER = "chatter"
NEW_THREAD = "new-thread"


class NewThread(BaseModel):
    id: str = Field(description='"new-1", "new-2", ... numbered from the index given in the prompt')
    title: str = Field(description="At most 5 plain words naming the concrete topic")
    description: str = Field(description="1-2 sentences: what this thread is about")


class Assignment(BaseModel):
    message_id: str = Field(description="The id= value of one message from the prompt")
    thread: str = Field(description='An existing thread slug, a "new-N" id you declared, or "chatter"')


class AssignmentResponse(BaseModel):
    new_threads: list[NewThread] = []
    assignments: list[Assignment]


class ItemOut(BaseModel):
    kind: Literal["task", "decision", "resource", "question"]
    text: str
    src_ids: list[str] = Field(description="Message ids (from [src:: ...]) this item comes from")
    owner: str | None = Field(default=None, description="A member name exactly as listed in the prompt")
    due: str | None = Field(default=None, description="YYYY-MM-DD, only if stated")
    done: bool = False
    block_id: str | None = Field(default=None, description="Reuse an existing item's ^id when updating it")


class ThreadUpdate(BaseModel):
    title: str
    summary: str
    items: list[ItemOut] = []


@dataclass
class SenderNames:
    """Per-batch resolution of sender jid -> wiki member title."""

    members: dict[str, str | None] = field(default_factory=dict)

    def wiki_name(self, m: BufferedMessage) -> str:
        title = self.members.get(m.sender)
        if title:
            return f"[[{title}]]"
        return m.sender_name or m.sender

    def prompt_name(self, m: BufferedMessage) -> str:
        title = self.members.get(m.sender)
        name = title or m.sender_name
        return f"{name} ({m.sender})" if name else m.sender

    def member_titles(self, messages: list[BufferedMessage]) -> list[str]:
        seen: list[str] = []
        for m in messages:
            title = self.members.get(m.sender)
            if title and title not in seen:
                seen.append(title)
        return seen


@dataclass
class AssignmentResult:
    buckets: dict[str, list[BufferedMessage]] = field(default_factory=dict)
    new_threads: dict[str, NewThread] = field(default_factory=dict)
    model_calls: int = 0
    unassigned: int = 0
