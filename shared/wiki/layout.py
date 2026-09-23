"""Where pages live (Wiki Format, Part 2). The one place a vault path is
spelled out - callers ask for a page by what it is, never build paths.
"""
from __future__ import annotations

from typing import Protocol

from shared.wiki.templates import slugify


class ChannelLike(Protocol):
    """What the wiki needs to know about a Channel - satisfied by the
    `channels` table row without importing it."""

    jid: str
    title: str | None
    kind: str
    initiative: str | None


def channel_dir(channel: ChannelLike) -> str:
    """The Channel's directory name under `channels/`, from its title (the
    jid until a `/setup` gives it one)."""
    return slugify(channel.title or channel.jid)


def channel_page_path(channel: ChannelLike) -> str:
    return f"channels/{channel_dir(channel)}.md"


def thread_dir(channel: ChannelLike) -> str:
    return f"channels/{channel_dir(channel)}"


def archive_dir(channel: ChannelLike) -> str:
    return f"channels/archive/{channel_dir(channel)}"


def initiative_page_path(kind: str, title: str) -> str:
    """`projects/<slug>.md` for a project, `events/<slug>.md` for an event."""
    return f"{'projects' if kind == 'project' else 'events'}/{slugify(title)}.md"


def member_page_path(title: str) -> str:
    return f"people/{slugify(title)}.md"


def inbox_page_path(agent: str) -> str:
    return f"inbox/{agent}.md"
