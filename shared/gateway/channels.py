"""Channel, member-registry, and outbound persistence behind Gateway DTOs."""
from __future__ import annotations

from sqlalchemy import select

from shared.db import BotAdmin, Channel, MembersRegistry, OutboundLog, get_session
from shared.gateway.types import ChannelInfo, MemberLink


def _channel_info(row: Channel) -> ChannelInfo:
    return ChannelInfo(
        jid=row.jid,
        title=row.title,
        kind=row.kind,
        initiative=row.initiative,
        cursor=row.cursor,
    )


def _member_link(row: MembersRegistry) -> MemberLink:
    return MemberLink(
        wa_identity=row.wa_identity,
        member_title=row.member_title,
        cms_member_id=row.cms_member_id,
    )


async def is_bot_admin(wa_identity: str) -> bool:
    async with get_session() as session:
        row = await session.get(BotAdmin, wa_identity)
    return row is not None


async def get_channel(jid: str) -> ChannelInfo | None:
    async with get_session() as session:
        row = await session.scalar(select(Channel).where(Channel.jid == jid))
    return _channel_info(row) if row is not None else None


async def get_channel_by_kind(kind: str) -> ChannelInfo | None:
    async with get_session() as session:
        row = await session.scalar(select(Channel).where(Channel.kind == kind))
    return _channel_info(row) if row is not None else None


async def list_channels(kind: str | None = None) -> list[ChannelInfo]:
    async with get_session() as session:
        stmt = select(Channel).where(Channel.kind != "unset")
        if kind:
            stmt = stmt.where(Channel.kind == kind)
        rows = await session.scalars(stmt)
    return [_channel_info(row) for row in rows]


async def is_allowlisted(jid: str) -> bool:
    channel = await get_channel(jid)
    return channel is not None and channel.kind != "unset"


async def upsert_channel(jid: str, kind: str, title: str | None, initiative: str | None) -> None:
    async with get_session() as session:
        channel = await session.scalar(select(Channel).where(Channel.jid == jid))
        if channel is None:
            session.add(Channel(jid=jid, kind=kind, title=title, initiative=initiative))
        else:
            channel.kind = kind
            channel.title = title
            channel.initiative = initiative
        await session.commit()


async def unwatch_channel(jid: str) -> bool:
    """Mark a channel unset. Returns False if it wasn't being watched."""
    async with get_session() as session:
        channel = await session.scalar(select(Channel).where(Channel.jid == jid))
        if channel is None or channel.kind == "unset":
            return False
        channel.kind = "unset"
        channel.initiative = None
        await session.commit()
    return True


async def advance_channel_cursor(jid: str, last_message_id: str) -> None:
    async with get_session() as session:
        channel = await session.scalar(select(Channel).where(Channel.jid == jid))
        if channel is not None:
            channel.cursor = last_message_id
            await session.commit()


async def is_bot_outbound(message_id: str) -> bool:
    async with get_session() as session:
        row = await session.scalar(select(OutboundLog).where(OutboundLog.message_id == message_id))
    return row is not None


async def link_member(
    wa_identity: str, member_title: str, cms_member_id: str | None = None, linked_by: str | None = None
) -> None:
    async with get_session() as session:
        row = await session.get(MembersRegistry, wa_identity)
        if row is None:
            session.add(
                MembersRegistry(
                    wa_identity=wa_identity,
                    member_title=member_title,
                    cms_member_id=cms_member_id,
                    linked_by=linked_by,
                )
            )
        else:
            row.member_title = member_title
            row.linked_by = linked_by
            if cms_member_id is not None:
                row.cms_member_id = cms_member_id
        await session.commit()


async def resolve_sender(wa_identity: str) -> MemberLink | None:
    async with get_session() as session:
        row = await session.get(MembersRegistry, wa_identity)
    return _member_link(row) if row is not None else None


async def admin_and_channel_counts() -> tuple[int, int]:
    async with get_session() as session:
        admin_count = len(list(await session.scalars(select(BotAdmin))))
        channel_count = len(list(await session.scalars(select(Channel).where(Channel.kind != "unset"))))
    return admin_count, channel_count
