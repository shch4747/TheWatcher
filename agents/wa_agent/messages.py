"""Dataclasses shared by the WA Agent's ingestion modules (`interface.py`
and `assign.py`) - a leaf module so neither has to import the other for
them. Nothing outside `agents/wa_agent` imports this directly
(ADR-0003); `interface.py` re-exports what tests and main.py need.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class BufferedMessage:
    row_id: int
    message_id: str
    channel: str
    received_at: datetime
    text: str
    sender: str
    replied_to_id: str | None = None
    # WhatsApp display name from gowa (`sender_display_name`/`from_name`),
    # None when gowa didn't send one. The wiki-side name comes from the
    # members registry first; this is the fallback (never the number).
    sender_name: str | None = None
    # When the message was actually sent (gowa's `timestamp`), as opposed
    # to received_at, which is when *we* buffered it - for a backfilled
    # message those differ by however long the history is.
    sent_at: datetime | None = None
    # Raw gowa message ids this logical message represents - itself
    # plus any same-sender plain messages concatenated onto it
    # (_concatenate_same_sender). Populated with [message_id] the first
    # time a BufferedMessage is built; every [src:: ...] tag and
    # processed-marking downstream uses this, not message_id alone.
    src_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.src_ids:
            self.src_ids = [self.message_id]
        if self.sent_at is None:
            self.sent_at = self.received_at

    @property
    def when(self) -> datetime:
        """The timestamp to show humans and models - sent_at, which
        __post_init__ guarantees is set."""
        assert self.sent_at is not None
        return self.sent_at


@dataclass
class ThreadInfo:
    slug: str
    path: str
    title: str
    summary: str
    state: str
    # Last few Timeline lines, capped at settings.thread_context_max_chars
    # - what the assignment prompt shows as "Recent:" for this thread.
    recent_context: str = ""
