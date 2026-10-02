"""Channel onboarding: /setup, setup-session replies, /unwatch."""
from __future__ import annotations

import logging

from sqlalchemy import select

from shared.db import SetupSession, get_session
from shared.gateway.channels import (
    get_channel,
    get_channel_by_kind,
    is_bot_admin,
    unwatch_channel,
    upsert_channel,
)
from shared.gateway.commands import SetupCommand
from shared.gateway.types import ChannelInfo
from shared.wiki.interface import (
    PageExists,
    ThreadStore,
    VaultClient,
    initiative_page_path,
    read_if_exists,
    render_new_event_page,
    render_new_project_page,
)

logger = logging.getLogger(__name__)

SETUP_STEPS = ["lead", "brief", "timeline"]

_DEFAULT_KIND_TITLES = {
    "coordis": "Coordis",
    "exes": "Exes",
    "research": "Research",
    "all": "All",
    "logs": "Logs",
    "other": "Other",
}


async def register_channel(
    vault: VaultClient, jid: str, kind: str, title: str, initiative: str | None
) -> None:
    """Allowlist the Channel and make sure it has a Channel Page."""
    old = await get_channel(jid)
    await upsert_channel(jid, kind, title, initiative)
    new = await get_channel(jid)
    assert new is not None
    if old is not None:
        left_behind = await ThreadStore.relocate(vault, old, new)
        if left_behind:
            logger.warning("retitling %s left pages in place (targets existed): %s", jid, left_behind)
    await ThreadStore(vault, new).ensure_channel_page()


async def setup(channel_jid: str, requested_by: str, cmd: SetupCommand, vault: VaultClient) -> str:
    """`/setup [kind] [<Title>]`."""
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /setup."

    if cmd.kind in ("coordis", "exes", "research", "all", "logs", "other"):
        return await _setup_singleton_or_other(channel_jid, cmd, vault)

    return await _setup_initiative(channel_jid, requested_by, cmd, vault)


async def _setup_singleton_or_other(channel_jid: str, cmd: SetupCommand, vault: VaultClient) -> str:
    if cmd.kind != "other":
        existing = await get_channel_by_kind(cmd.kind)
        if existing is not None and existing.jid != channel_jid:
            return f"A {cmd.kind} channel is already set up; refusing a second one."
    title = cmd.title or _DEFAULT_KIND_TITLES[cmd.kind]
    await register_channel(vault, channel_jid, cmd.kind, title, initiative=None)
    return "watching"


async def _setup_initiative(
    channel_jid: str, requested_by: str, cmd: SetupCommand, vault: VaultClient
) -> str:
    if not cmd.title:
        return f"/setup {cmd.kind} needs a title: /setup {cmd.kind} <Title>"

    if await read_if_exists(vault, initiative_page_path(cmd.kind, cmd.title)) is None:
        async with get_session() as session:
            session.add(
                SetupSession(channel=channel_jid, kind=cmd.kind, title=cmd.title, requested_by=requested_by)
            )
            await session.commit()
        return f'Setting up "{cmd.title}" as a new {cmd.kind}. Who is the lead? (reply with their name)'

    await register_channel(vault, channel_jid, cmd.kind, cmd.title, initiative=cmd.title)
    return "watching"


async def _active_setup_session(channel_jid: str) -> SetupSession | None:
    async with get_session() as session:
        return await session.scalar(
            select(SetupSession)
            .where(SetupSession.channel == channel_jid, SetupSession.step != "done")
            .order_by(SetupSession.created_at.desc())
        )


async def continue_setup_session(channel_jid: str, reply_text: str, vault: VaultClient) -> str | None:
    """Feed one chat reply into the in-progress setup_session for this channel."""
    session_row = await _active_setup_session(channel_jid)
    if session_row is None:
        return None

    async with get_session() as session:
        row = await session.get(SetupSession, session_row.id)
        assert row is not None
        setattr(row, row.step, reply_text.strip())
        idx = SETUP_STEPS.index(row.step)
        if idx + 1 < len(SETUP_STEPS):
            row.step = SETUP_STEPS[idx + 1]
            await session.commit()
            next_field = row.step
            return f"Got it. What's the {next_field}?"

        row.step = "done"
        lead, brief, timeline = row.lead, row.brief, row.timeline
        kind, title, requested_by = row.kind, row.title, row.requested_by
        await session.commit()

    lead_name = lead or requested_by
    render = render_new_project_page if kind == "project" else render_new_event_page
    try:
        page = render(title, lead=lead_name, brief=brief or "")
        await vault.create(initiative_page_path(kind, title), page)
    except PageExists:
        pass
    await register_channel(vault, channel_jid, kind, title, initiative=title)
    return f'"{title}" created (timeline: {timeline}). watching'


async def unwatch(channel_jid: str, requested_by: str, target_jid: str | None = None) -> str:
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /unwatch."
    target = target_jid or channel_jid
    watched = await unwatch_channel(target)
    if not watched:
        return f"{target} wasn't being watched." if target_jid else "This channel wasn't being watched."
    return f"No longer watching {target}." if target_jid else "No longer watching this channel."


def channel_status_line(channel: ChannelInfo | None) -> str:
    if channel is None or channel.kind == "unset":
        return "Not watching this channel."
    return f"kind={channel.kind} initiative={channel.initiative or '-'} cursor={channel.cursor or '-'}"
