"""Per-type frontmatter schemas (Wiki Format, Part 3) and the section-owner
tables every parser/lint/regeneration job needs. Unknown frontmatter keys
are preserved by the parser, never validated away (Wiki Format, principle 1).
"""
from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Owner = Literal["human", "shared", "managed", "append", "derived"]


class Frontmatter(BaseModel):
    """Common fields on every page type. Subclasses add their own; unknown
    keys are kept separately by the parser (see parser.Page.extra_frontmatter).
    """

    model_config = ConfigDict(extra="allow")

    type: str
    slug: str
    title: str
    created: datetime | None = None
    updated: datetime | None = None
    updated_by: str | None = None
    tags: list[str] = []


class MemberFrontmatter(Frontmatter):
    type: Literal["member"] = "member"
    role: str
    status: Literal["active", "alumni"] = "active"
    whatsapp: Literal["linked", "unlinked"] = "unlinked"


class ProjectFrontmatter(Frontmatter):
    type: Literal["project"] = "project"
    status: Literal["proposed", "active", "paused", "done", "dead"] = "proposed"
    lead: str
    members: list[str] = []
    channel: str | None = None
    started: date_type | None = None
    target: date_type | None = None
    category: str | None = None
    repos: list[str] = []
    kanban: str | None = None
    health: float | None = None
    health_updated: date_type | None = None


class EventFrontmatter(Frontmatter):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: Literal["event"] = "event"
    status: Literal["proposed", "active", "paused", "done", "dead"] = "proposed"
    lead: str
    members: list[str] = []
    channel: str | None = None
    event_type: str | None = None
    event_date: date_type | None = Field(default=None, alias="date")
    end_date: date_type | None = None
    venue: str | None = None
    registrations: str | None = None
    budget: str | None = None


class ChannelFrontmatter(Frontmatter):
    type: Literal["channel"] = "channel"
    kind: Literal["coordis", "exes", "research", "all", "project", "event", "other", "logs"]
    initiative: str | None = None
    cursor: str | None = None
    last_batch: datetime | None = None


class ThreadFrontmatter(Frontmatter):
    type: Literal["thread"] = "thread"
    channel: str
    initiative: str | None = None
    state: Literal["active", "stale", "ended"] = "active"
    opened_at: datetime | None = None
    last_message_at: datetime | None = None
    participants: list[str] = []
    message_ids: list[str] = []
    continues: str | None = None
    summary_cursor: str | None = None


class ResourceFrontmatter(Frontmatter):
    type: Literal["resource"] = "resource"
    kind: Literal["paper", "repo", "talk", "article", "note"]
    url: str | None = None
    related: list[str] = []
    added_by: str | None = None


class InboxFrontmatter(Frontmatter):
    """An agent's Inbox (`inbox/<agent>.md`) - the source of truth for
    what that agent still has to look at (ADR-0014)."""

    type: Literal["inbox"] = "inbox"
    agent: str


SCHEMA_BY_TYPE: dict[str, type[Frontmatter]] = {
    "member": MemberFrontmatter,
    "project": ProjectFrontmatter,
    "event": EventFrontmatter,
    "channel": ChannelFrontmatter,
    "thread": ThreadFrontmatter,
    "resource": ResourceFrontmatter,
    "inbox": InboxFrontmatter,
}


# Section owners per type (Wiki Format, Part 3). Keys are canonical H2
# titles; ALIASES maps a lowercased alias to its canonical title.
SECTION_OWNERS: dict[str, dict[str, Owner]] = {
    "member": {
        "About": "human",
        "Projects": "derived",
        "Past projects": "derived",
        "Open tasks": "derived",
        "Notes": "shared",
    },
    "project": {
        "Brief": "human",
        "Status": "managed",
        "Open tasks": "managed",
        "Decisions": "append",
        "Log": "append",
        "Resources": "append",
        "Threads": "derived",
        "Notes": "shared",
    },
    "event": {
        "Brief": "human",
        "Status": "managed",
        "Logistics": "managed",
        "Decisions": "append",
        "Log": "append",
        "Resources": "append",
        "Threads": "derived",
        "Outcome": "human",
        "Notes": "shared",
    },
    "channel": {
        "Active threads": "derived",
        "Stale threads": "derived",
        "Archived threads": "derived",
        "Notes": "shared",
    },
    "thread": {
        "Summary": "managed",
        "Items": "managed",
        "Timeline": "append",
        "Notes": "shared",
    },
    "resource": {
        "Summary": "managed",
        "Notes": "human",
    },
    # Pending and Done are rewritten by shared/inbox only; a human may
    # still add a line to Pending by hand and it is picked up (ADR-0014).
    "inbox": {
        "Pending": "managed",
        "Done": "managed",
        "Notes": "shared",
    },
}

SECTION_ALIASES: dict[str, str] = {
    "tasks": "Open tasks",
    "to do": "Open tasks",
    "todo": "Open tasks",
}


def canonical_section_title(page_type: str, title: str) -> str:
    """Resolve an alias to its canonical title for this page type; titles
    with no registered alias are returned unchanged (Wiki Format, principle 2:
    unknown sections are preserved verbatim, not rejected)."""
    owners = SECTION_OWNERS.get(page_type, {})
    for canonical in owners:
        if canonical.lower() == title.lower():
            return canonical
    alias_target = SECTION_ALIASES.get(title.lower())
    if alias_target and alias_target in owners:
        return alias_target
    return title


def owner_of(page_type: str, section_title: str) -> Owner | None:
    canonical = canonical_section_title(page_type, section_title)
    return SECTION_OWNERS.get(page_type, {}).get(canonical)
