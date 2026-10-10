"""Database logic for tracking and revoking /setup-members auto-links (❌ reaction)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from shared.db import AutoLinkNotice, MembersRegistry, RejectedLink, get_session


@dataclass(frozen=True)
class UndoResult:
    channel: str
    member_title: str
    removed: int
    kept: int


async def record_notice(
    message_id: str,
    channel: str,
    member_title: str,
    cms_member_id: str | None,
    identities: list[str],
) -> None:
    """Store an active notice for an auto-linked member."""
    clean_identities = [ident.strip() for ident in identities if ident and ident.strip()]
    async with get_session() as session:
        notice = AutoLinkNotice(
            message_id=message_id,
            channel=channel,
            member_title=member_title,
            cms_member_id=cms_member_id,
            identities=json.dumps(clean_identities),
            status="active",
            created_at=datetime.now(UTC),
        )
        session.add(notice)
        await session.commit()


async def is_rejected(identities: list[str], member_title: str) -> bool:
    """Return True if any of the given identities has been rejected for this member title."""
    clean_identities = [ident.strip() for ident in identities if ident and ident.strip()]
    if not clean_identities:
        return False
    async with get_session() as session:
        result = await session.scalar(
            select(RejectedLink).where(
                RejectedLink.member_title == member_title,
                RejectedLink.wa_identity.in_(clean_identities),
            )
        )
        return result is not None


async def undo_notice(message_id: str, undone_by: str) -> UndoResult | None:
    """Revert an auto-link notice if active, removing matched registry entries and recording rejections."""
    async with get_session() as session:
        notice = await session.get(AutoLinkNotice, message_id)
        if notice is None or notice.status != "active":
            return None

        try:
            identities_list: list[str] = json.loads(notice.identities)
        except Exception:
            identities_list = []

        now = datetime.now(UTC)
        removed = 0
        kept = 0

        clean_identities = [ident.strip() for ident in identities_list if ident and ident.strip()]
        for ident in clean_identities:
            reg_row = await session.get(MembersRegistry, ident)
            if reg_row is not None:
                if (
                    reg_row.member_title == notice.member_title
                    and reg_row.cms_member_id == notice.cms_member_id
                ):
                    await session.delete(reg_row)
                    removed += 1
                else:
                    kept += 1

            existing_rejection = await session.get(
                RejectedLink, {"wa_identity": ident, "member_title": notice.member_title}
            )
            if existing_rejection is None:
                session.add(
                    RejectedLink(
                        wa_identity=ident,
                        member_title=notice.member_title,
                        rejected_by=undone_by,
                        rejected_at=now,
                    )
                )

        notice.status = "undone"
        notice.resolved_at = now
        notice.resolved_by = undone_by

        await session.commit()

        return UndoResult(
            channel=notice.channel,
            member_title=notice.member_title,
            removed=removed,
            kept=kept,
        )
