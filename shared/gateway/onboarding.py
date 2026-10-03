"""Channel onboarding: /setup, setup-session replies, /unwatch.

A project or event channel is linked to its initiative page (`projects/`
or `events/`). If that page doesn't exist yet, `/setup` asks the admin for
a lead, a brief and a timeline in chat, creates the page from the answers,
and only then watches the channel. Once a channel is watched, the optional
`on_watching` callback runs (the runtime uses it to import chat history).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select, update

from shared.cms.interface import MEMBER_MATCH_THRESHOLD, default_cms_client, rank_member
from shared.db import SetupSession, get_session
from shared.gateway.channels import (
    get_channel,
    get_channel_by_kind,
    is_bot_admin,
    resolve_sender,
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
CANCELLED_STEP = "cancelled"

# Runs once a channel is watched; returns a line to append to the reply.
WatchedCallback = Callable[[ChannelInfo], Awaitable[str | None]]

UNASSIGNED_LEAD = "Unassigned"
_SELF_WORDS = frozenset({"me", "myself", "i", "self"})
_SKIP_WORDS = frozenset({"skip", "-", "n/a", "na", "none", "later"})
_WIKILINK_UNSAFE = re.compile(r'[\[\]"\n\r]')

_DEFAULT_KIND_TITLES = {
    "coordis": "Coordis",
    "exes": "Exes",
    "research": "Research",
    "all": "All",
    "logs": "Logs",
    "other": "Other",
}

_PROMPTS = {
    "brief": 'Got it. What\'s the brief? A sentence or two on what "{title}" is about (or "skip").',
    "timeline": 'Thanks. What\'s the timeline? {hint} (or "skip")',
}
_TIMELINE_HINTS = {
    "project": "Target dates or milestones.",
    "event": "When does it happen, and any deadlines before it?",
}


@dataclass(frozen=True)
class InitiativeContext:
    """What the admin told us about a new project/event, ready to become
    its page. The timeline has no frontmatter field of its own, so it is
    kept at the end of the human-owned Brief."""

    kind: str
    title: str
    lead: str
    brief: str = ""
    timeline: str = ""

    def brief_section(self) -> str:
        timeline = f"**Timeline:** {self.timeline}" if self.timeline else ""
        return "\n\n".join(part for part in (self.brief, timeline) if part)

    def render_page(self) -> str:
        render = render_new_project_page if self.kind == "project" else render_new_event_page
        return render(self.title, lead=self.lead, brief=self.brief_section())


def _answer(reply: str | None) -> str:
    """A reply as page text: a "skip" is no answer at all."""
    text = (reply or "").strip()
    return "" if text.lower() in _SKIP_WORDS else text


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


async def _watching_reply(
    channel_jid: str, on_watching: WatchedCallback | None, prefix: str = "watching"
) -> str:
    """The success reply, with whatever the callback reports on a second line."""
    channel = await get_channel(channel_jid)
    if on_watching is None or channel is None:
        return prefix
    try:
        note = await on_watching(channel)
    except Exception:  # noqa: BLE001 - the channel is set up; a follow-up failing must not say otherwise
        logger.exception("post-setup step failed for %s", channel_jid)
        note = None
    return f"{prefix}\n{note}" if note else prefix


async def setup(
    channel_jid: str,
    requested_by: str,
    cmd: SetupCommand,
    vault: VaultClient,
    on_watching: WatchedCallback | None = None,
) -> str:
    """`/setup [kind] [<Title>]`."""
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /setup."

    # A new /setup supersedes any dialogue still waiting on this channel,
    # otherwise its next answer would be swallowed by the stale one.
    await _cancel_setup_sessions(channel_jid)

    if cmd.kind in ("coordis", "exes", "research", "all", "logs", "other"):
        return await _setup_singleton_or_other(channel_jid, cmd, vault, on_watching)

    return await _setup_initiative(channel_jid, requested_by, cmd, vault, on_watching)


async def _setup_singleton_or_other(
    channel_jid: str, cmd: SetupCommand, vault: VaultClient, on_watching: WatchedCallback | None
) -> str:
    if cmd.kind != "other":
        existing = await get_channel_by_kind(cmd.kind)
        if existing is not None and existing.jid != channel_jid:
            return f"A {cmd.kind} channel is already set up; refusing a second one."
    title = cmd.title or _DEFAULT_KIND_TITLES[cmd.kind]
    await register_channel(vault, channel_jid, cmd.kind, title, initiative=None)
    return await _watching_reply(channel_jid, on_watching)


async def _setup_initiative(
    channel_jid: str,
    requested_by: str,
    cmd: SetupCommand,
    vault: VaultClient,
    on_watching: WatchedCallback | None,
) -> str:
    if not cmd.title:
        return f"/setup {cmd.kind} needs a title: /setup {cmd.kind} <Title>"

    if await read_if_exists(vault, initiative_page_path(cmd.kind, cmd.title)) is None:
        async with get_session() as session:
            session.add(
                SetupSession(channel=channel_jid, kind=cmd.kind, title=cmd.title, requested_by=requested_by)
            )
            await session.commit()
        return (
            f'Setting up "{cmd.title}" as a new {cmd.kind}. Who is the lead? '
            '(reply with their name, tag them, or say "me")'
        )

    await register_channel(vault, channel_jid, cmd.kind, cmd.title, initiative=cmd.title)
    return await _watching_reply(channel_jid, on_watching)


async def _cancel_setup_sessions(channel_jid: str) -> None:
    async with get_session() as session:
        await session.execute(
            update(SetupSession)
            .where(SetupSession.channel == channel_jid, SetupSession.step.in_(SETUP_STEPS))
            .values(step=CANCELLED_STEP)
        )
        await session.commit()


async def _active_setup_session(channel_jid: str) -> SetupSession | None:
    async with get_session() as session:
        return await session.scalar(
            select(SetupSession)
            .where(SetupSession.channel == channel_jid, SetupSession.step.in_(SETUP_STEPS))
            .order_by(SetupSession.created_at.desc(), SetupSession.id.desc())
        )


async def has_setup_session(channel_jid: str, sender: str) -> bool:
    """Is `sender` the admin a setup dialogue in this channel is waiting on?
    The webhook uses this to let their (non-command) answers through
    before the channel is allowlisted."""
    session_row = await _active_setup_session(channel_jid)
    return session_row is not None and session_row.requested_by == sender


async def _resolve_lead(reply: str, requested_by: str, mentions: list[str] | None) -> str:
    """A lead as a member title where we can tell who they mean (a tag,
    "me", or a name close to the ARIES roster), else what was typed. Never
    a phone number or JID - nothing like that belongs in the wiki."""
    for mention in mentions or []:
        linked = await resolve_sender(mention)
        if linked is not None:
            return linked.member_title
    text = _answer(reply)
    if not text or text.lower() in _SELF_WORDS:
        linked = await resolve_sender(requested_by)
        return linked.member_title if linked else UNASSIGNED_LEAD
    if text.startswith("@") and not any(ch.isalpha() for ch in text):
        return UNASSIGNED_LEAD  # an @number tag that matched no linked member
    try:
        ranked = rank_member(text, await default_cms_client().list_members())
    except Exception:  # noqa: BLE001 - the roster being down must not stall setup
        ranked = None
    if ranked is not None and ranked[1] >= MEMBER_MATCH_THRESHOLD:
        return ranked[0].title
    return _WIKILINK_UNSAFE.sub("", text).strip() or UNASSIGNED_LEAD


def _next_prompt(step: str, kind: str, title: str) -> str:
    template = _PROMPTS[step]
    return template.format(title=title, hint=_TIMELINE_HINTS.get(kind, ""))


async def continue_setup_session(
    channel_jid: str,
    reply_text: str,
    vault: VaultClient,
    on_watching: WatchedCallback | None = None,
    mentions: list[str] | None = None,
) -> str | None:
    """Feed one chat reply into the in-progress setup_session for this channel."""
    session_row = await _active_setup_session(channel_jid)
    if session_row is None:
        return None

    answer = reply_text.strip()
    if session_row.step == "lead":
        answer = await _resolve_lead(reply_text, session_row.requested_by, mentions)

    async with get_session() as session:
        row = await session.get(SetupSession, session_row.id)
        assert row is not None
        setattr(row, row.step, answer)
        idx = SETUP_STEPS.index(row.step)
        if idx + 1 < len(SETUP_STEPS):
            row.step = SETUP_STEPS[idx + 1]
            await session.commit()
            return _next_prompt(row.step, row.kind, row.title)

        row.step = "done"
        context = InitiativeContext(
            kind=row.kind,
            title=row.title,
            lead=row.lead or UNASSIGNED_LEAD,
            brief=_answer(row.brief),
            timeline=_answer(row.timeline),
        )
        await session.commit()

    try:
        await vault.create(initiative_page_path(context.kind, context.title), context.render_page())
    except PageExists:
        pass
    await register_channel(vault, channel_jid, context.kind, context.title, initiative=context.title)
    path = initiative_page_path(context.kind, context.title).removesuffix(".md")
    return await _watching_reply(
        channel_jid, on_watching, prefix=f'"{context.title}" created at {path} and linked. watching'
    )


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
