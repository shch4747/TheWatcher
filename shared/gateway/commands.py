"""Command parsing (Spec: "Commands are parsed by regex, never a model").
Accepted only from Bot Admins - that check happens in interface.py, not
here; this module only recognizes shape."""
from __future__ import annotations

import re
from dataclasses import dataclass

SINGLETON_KINDS = {"coordis", "exes", "research", "all"}
SETUP_KINDS = {"project", "event", "coordis", "exes", "research", "all", "other"}


@dataclass(frozen=True)
class SetupCommand:
    kind: str
    title: str | None


@dataclass(frozen=True)
class UnwatchCommand:
    pass


@dataclass(frozen=True)
class StatusCommand:
    pass


@dataclass(frozen=True)
class LinkCommand:
    sender_ref: str
    member_ref: str


Command = SetupCommand | UnwatchCommand | StatusCommand | LinkCommand

_SETUP_RE = re.compile(
    r"^/setup(?:\s+(?P<kind>project|event|coordis|exes|research|all|other))?(?:\s+(?P<title>.+))?\s*$",
    re.IGNORECASE,
)
_UNWATCH_RE = re.compile(r"^/unwatch\s*$", re.IGNORECASE)
_STATUS_RE = re.compile(r"^/status\s*$", re.IGNORECASE)
_LINK_RE = re.compile(r"^/link\s+(?P<sender>\S+)\s+(?P<member>\[\[[^\]]+\]\])\s*$", re.IGNORECASE)


def parse_command(text: str) -> Command | None:
    text = text.strip()
    if not text.startswith("/"):
        return None

    if m := _SETUP_RE.match(text):
        kind = (m.group("kind") or "other").lower()
        title = m.group("title").strip() if m.group("title") else None
        return SetupCommand(kind=kind, title=title)
    if _UNWATCH_RE.match(text):
        return UnwatchCommand()
    if _STATUS_RE.match(text):
        return StatusCommand()
    if m := _LINK_RE.match(text):
        return LinkCommand(sender_ref=m.group("sender"), member_ref=m.group("member"))
    return None


def is_group_jid(jid: str) -> bool:
    return jid.endswith("@g.us")
