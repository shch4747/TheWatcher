"""Proposal rows (ADR-0007). Persistence lives with the Gateway; the
WhatsApp Agent owns the lifecycle and executors."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from shared.db import Proposal, aware_utc, get_session


@dataclass(frozen=True)
class ProposalRecord:
    id: int
    channel: str
    kind: str
    payload: dict
    status: str
    message_id: str | None
    expires_at: datetime | None


def _to_record(row: Proposal) -> ProposalRecord:
    return ProposalRecord(
        id=row.id,
        channel=row.channel,
        kind=row.kind,
        payload=json.loads(row.payload),
        status=row.status,
        message_id=row.message_id,
        expires_at=aware_utc(row.expires_at),
    )


class ProposalStore:
    async def create(self, channel: str, kind: str, payload: dict, expires_at: datetime) -> int:
        async with get_session() as session:
            proposal = Proposal(
                channel=channel, kind=kind, payload=json.dumps(payload), expires_at=expires_at
            )
            session.add(proposal)
            await session.commit()
            return proposal.id

    async def set_message_id(self, proposal_id: int, message_id: str | None) -> None:
        async with get_session() as session:
            row = await session.get(Proposal, proposal_id)
            if row is not None:
                row.message_id = message_id
                await session.commit()

    async def get(self, proposal_id: int) -> ProposalRecord | None:
        async with get_session() as session:
            row = await session.get(Proposal, proposal_id)
        return _to_record(row) if row is not None else None

    async def get_pending_by_message(self, message_id: str) -> ProposalRecord | None:
        async with get_session() as session:
            row = await session.scalar(
                select(Proposal).where(Proposal.message_id == message_id, Proposal.status == "pending")
            )
        return _to_record(row) if row is not None else None

    async def mark_expired(self, proposal_id: int) -> None:
        async with get_session() as session:
            row = await session.get(Proposal, proposal_id)
            if row is not None:
                row.status = "expired"
                await session.commit()

    async def mark_confirmed(self, proposal_id: int, confirmed_by: str, resolved_at: datetime) -> None:
        async with get_session() as session:
            row = await session.get(Proposal, proposal_id)
            if row is not None:
                row.status = "confirmed"
                row.resolved_at = resolved_at
                row.resolved_by = confirmed_by
                await session.commit()

    async def expire_due(self, now: datetime) -> list[ProposalRecord]:
        async with get_session() as session:
            pending = list(await session.scalars(select(Proposal).where(Proposal.status == "pending")))
            expired_rows = []
            for p in pending:
                expires_at = aware_utc(p.expires_at)
                if expires_at is not None and expires_at < now:
                    p.status = "expired"
                    expired_rows.append(p)
            await session.commit()
            return [_to_record(p) for p in expired_rows]

    async def pending_count(self) -> int:
        async with get_session() as session:
            return len(list(await session.scalars(select(Proposal).where(Proposal.status == "pending"))))
