"""Gateway-facing DTOs. Callers never see SQLAlchemy rows."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class ChannelInfo:
    """What every other package may know about a watched channel."""

    jid: str
    title: str | None
    kind: str
    initiative: str | None
    cursor: str | None = None


@dataclass(frozen=True)
class MemberLink:
    """A WhatsApp identity already linked to a wiki member."""

    wa_identity: str
    member_title: str
    cms_member_id: str | None = None


class SendResult(BaseModel):
    message_id: str | None
