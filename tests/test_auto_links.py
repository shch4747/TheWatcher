"""Tests for shared/gateway/auto_links.py tracking and undo logic."""
from __future__ import annotations

import pytest
from shared.db import AutoLinkNotice, MembersRegistry, RejectedLink, get_session
from shared.gateway.auto_links import UndoResult, is_rejected, record_notice, undo_notice


@pytest.mark.asyncio
async def test_record_then_undo_removes_both_registry_rows():
    id_a = "t1-a@s.whatsapp.net"
    id_b = "t1-b@s.whatsapp.net"
    msg_id = "t1-msg-001"
    channel = "t1-chan@g.us"
    title = "Alice Test"
    cms_id = "t1-cms-123"
    admin = "t1-admin@s.whatsapp.net"

    async with get_session() as session:
        session.add(MembersRegistry(wa_identity=id_a, member_title=title, cms_member_id=cms_id))
        session.add(MembersRegistry(wa_identity=id_b, member_title=title, cms_member_id=cms_id))
        await session.commit()

    await record_notice(
        message_id=msg_id,
        channel=channel,
        member_title=title,
        cms_member_id=cms_id,
        identities=[id_a, id_b],
    )

    result = await undo_notice(msg_id, admin)
    assert result == UndoResult(channel=channel, member_title=title, removed=2, kept=0)

    async with get_session() as session:
        assert await session.get(MembersRegistry, id_a) is None
        assert await session.get(MembersRegistry, id_b) is None

        notice = await session.get(AutoLinkNotice, msg_id)
        assert notice is not None
        assert notice.status == "undone"
        assert notice.resolved_by == admin
        assert notice.resolved_at is not None

        rej_a = await session.get(RejectedLink, {"wa_identity": id_a, "member_title": title})
        rej_b = await session.get(RejectedLink, {"wa_identity": id_b, "member_title": title})
        assert rej_a is not None
        assert rej_a.rejected_by == admin
        assert rej_b is not None
        assert rej_b.rejected_by == admin


@pytest.mark.asyncio
async def test_second_undo_returns_none():
    id_a = "t2-a@s.whatsapp.net"
    msg_id = "t2-msg-002"
    channel = "t2-chan@g.us"
    title = "Bob Test"
    admin = "t2-admin@s.whatsapp.net"

    await record_notice(
        message_id=msg_id,
        channel=channel,
        member_title=title,
        cms_member_id=None,
        identities=[id_a],
    )

    first = await undo_notice(msg_id, admin)
    assert first is not None
    assert first.member_title == title

    second = await undo_notice(msg_id, admin)
    assert second is None


@pytest.mark.asyncio
async def test_undo_unknown_message_id_returns_none():
    res = await undo_notice("t3-nonexistent-msg", "t3-admin@s.whatsapp.net")
    assert res is None


@pytest.mark.asyncio
async def test_registry_row_with_different_member_title_is_kept():
    id_a = "t4-a@s.whatsapp.net"
    id_b = "t4-b@s.whatsapp.net"
    msg_id = "t4-msg-004"
    channel = "t4-chan@g.us"
    title = "Carol Test"
    diff_title = "Dave Other"
    cms_id = "t4-cms-456"
    admin = "t4-admin@s.whatsapp.net"

    async with get_session() as session:
        session.add(MembersRegistry(wa_identity=id_a, member_title=title, cms_member_id=cms_id))
        session.add(MembersRegistry(wa_identity=id_b, member_title=diff_title, cms_member_id=cms_id))
        await session.commit()

    await record_notice(
        message_id=msg_id,
        channel=channel,
        member_title=title,
        cms_member_id=cms_id,
        identities=[id_a, id_b],
    )

    result = await undo_notice(msg_id, admin)
    assert result == UndoResult(channel=channel, member_title=title, removed=1, kept=1)

    async with get_session() as session:
        assert await session.get(MembersRegistry, id_a) is None
        kept_row = await session.get(MembersRegistry, id_b)
        assert kept_row is not None
        assert kept_row.member_title == diff_title

        rej_a = await session.get(RejectedLink, {"wa_identity": id_a, "member_title": title})
        rej_b = await session.get(RejectedLink, {"wa_identity": id_b, "member_title": title})
        assert rej_a is not None
        assert rej_b is not None


@pytest.mark.asyncio
async def test_registry_row_with_different_cms_member_id_is_kept():
    id_a = "t5-a@s.whatsapp.net"
    msg_id = "t5-msg-005"
    channel = "t5-chan@g.us"
    title = "Eve Test"
    cms_id = "t5-cms-orig"
    diff_cms_id = "t5-cms-other"
    admin = "t5-admin@s.whatsapp.net"

    async with get_session() as session:
        session.add(MembersRegistry(wa_identity=id_a, member_title=title, cms_member_id=diff_cms_id))
        await session.commit()

    await record_notice(
        message_id=msg_id,
        channel=channel,
        member_title=title,
        cms_member_id=cms_id,
        identities=[id_a],
    )

    result = await undo_notice(msg_id, admin)
    assert result == UndoResult(channel=channel, member_title=title, removed=0, kept=1)

    async with get_session() as session:
        kept_row = await session.get(MembersRegistry, id_a)
        assert kept_row is not None
        assert kept_row.cms_member_id == diff_cms_id


@pytest.mark.asyncio
async def test_is_rejected_behavior():
    id_a = "t6-a@s.whatsapp.net"
    title = "Frank Test"
    diff_title = "Grace Test"
    admin = "t6-admin@s.whatsapp.net"

    assert not await is_rejected([id_a], title)
    assert not await is_rejected([], title)
    assert not await is_rejected(["", "   "], title)

    async with get_session() as session:
        session.add(
            RejectedLink(
                wa_identity=id_a,
                member_title=title,
                rejected_by=admin,
            )
        )
        await session.commit()

    assert await is_rejected([id_a], title)
    assert await is_rejected(["", id_a, "  "], title)
    assert not await is_rejected([id_a], diff_title)
    assert not await is_rejected(["t6-other@s.whatsapp.net"], title)


@pytest.mark.asyncio
async def test_record_notice_ignores_blank_identities():
    id_real = "t7-real@s.whatsapp.net"
    msg_id = "t7-msg-007"
    channel = "t7-chan@g.us"
    title = "Heidi Test"
    admin = "t7-admin@s.whatsapp.net"

    async with get_session() as session:
        session.add(MembersRegistry(wa_identity=id_real, member_title=title, cms_member_id=None))
        await session.commit()

    await record_notice(
        message_id=msg_id,
        channel=channel,
        member_title=title,
        cms_member_id=None,
        identities=["", "   ", id_real],
    )

    result = await undo_notice(msg_id, admin)
    assert result == UndoResult(channel=channel, member_title=title, removed=1, kept=0)

    async with get_session() as session:
        assert await session.get(MembersRegistry, id_real) is None
        rej = await session.get(RejectedLink, {"wa_identity": id_real, "member_title": title})
        assert rej is not None
