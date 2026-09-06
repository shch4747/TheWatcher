"""
Group-based context for the WA agent.

Two things the WA agent needs that are per-group, not global:

  1. INGEST cadence — "runs on N messages / X hours", personalizable per
     group (a busy project group vs a quiet announcements channel).

  2. OUTBOUND routing — when an agent finishes and triggers WA with an
     output, WA must decide WHICH group to send to. Agents know a
     project_ref or a topic, not a WhatsApp JID. The registry maps
     project -> group(s), and knows which groups are general announcement
     targets, so a logical audience ("project:dashboard", "announce")
     resolves to concrete JIDs.

Plus a rolling per-group summary (stored via WAMemory) so outbound messages
can be phrased in that group's context.

Config lives in the wiki at meta/groups.md (frontmatter), so non-devs can
edit it, e.g.:

    ---
    groups:
      - jid: 123-456@g.us
        name: Dashboard Team
        kind: project           # project | announce | events | coordi | exes
        projects: [dashboard]
        n_messages: 15
        max_age_s: 3600
      - jid: 999-888@g.us
        name: ARIES TLDR
        kind: announce
        announce: true
      - jid: 111-222@g.us
        name: ARIES Events
        kind: events
      - jid: 333-444@g.us
        name: ARIES Coordi Chat
        kind: coordi            # general chat, high spam — heavy preprocessing
      - jid: 555-666@g.us
        name: ARIES Exes Chat
        kind: exes              # general chat, high spam — heavy preprocessing
    ---
"""
from __future__ import annotations

from dataclasses import dataclass, field

import mdfront


DEFAULT_N_MESSAGES = 20
DEFAULT_MAX_AGE_S = 3600.0
GROUPS_PAGE = "meta/groups.md"


@dataclass
class GroupContext:
    jid: str
    name: str = ""
    kind: str = "project"                       # "project" | "announce" | "events" | "coordi" | "exes"
    projects: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    n_messages: int = DEFAULT_N_MESSAGES
    max_age_s: float = DEFAULT_MAX_AGE_S
    announce: bool = False                       # eligible as a broadcast target

    @property
    def is_announce(self) -> bool:
        return self.announce or self.kind == "announce"

    @property
    def is_events(self) -> bool:
        return self.kind == "events"

    @property
    def is_coordi(self) -> bool:
        """General chat group for club coordinators — high volume of casual
        messages / spam; needs aggressive preprocessing to extract signal."""
        return self.kind == "coordi"

    @property
    def is_exes(self) -> bool:
        """General chat group for club executives — same high-spam profile
        as coordi; needs aggressive preprocessing."""
        return self.kind == "exes"

    @property
    def is_noisy_group(self) -> bool:
        """True for group kinds with a high spam-to-signal ratio that need
        heavier preprocessing (coordi, exes)."""
        return self.kind in ("coordi", "exes")


class GroupRegistry:
    def __init__(self, groups: list[GroupContext]):
        self.groups = groups
        self.by_jid = {g.jid: g for g in groups}

    # -- construction ----------------------------------------------------

    @classmethod
    def from_list(cls, dicts: list[dict]) -> "GroupRegistry":
        return cls([GroupContext(**{k: v for k, v in d.items() if k in GroupContext.__annotations__})
                    for d in dicts])

    @classmethod
    def from_wiki(cls, client, path: str = GROUPS_PAGE) -> "GroupRegistry":
        raw = client.read_file(path)
        if not raw:
            return cls([])
        meta, _ = mdfront.parse(raw)
        return cls.from_list(meta.get("groups", []) or [])

    # -- ingest side -----------------------------------------------------

    def allowlist(self) -> set[str]:
        return set(self.by_jid)

    def get(self, jid: str) -> GroupContext | None:
        return self.by_jid.get(jid)

    def cadence_for(self, jid: str) -> tuple[int, float]:
        g = self.by_jid.get(jid)
        return (g.n_messages, g.max_age_s) if g else (DEFAULT_N_MESSAGES, DEFAULT_MAX_AGE_S)

    # -- outbound routing ------------------------------------------------

    def groups_for_project(self, project_ref: str) -> list[str]:
        return [g.jid for g in self.groups if project_ref in g.projects]

    def announce_groups(self) -> list[str]:
        return [g.jid for g in self.groups if g.is_announce]

    def events_groups(self) -> list[str]:
        return [g.jid for g in self.groups if g.is_events]

    def coordi_groups(self) -> list[str]:
        return [g.jid for g in self.groups if g.is_coordi]

    def exes_groups(self) -> list[str]:
        return [g.jid for g in self.groups if g.is_exes]

    def noisy_groups(self) -> set[str]:
        """JIDs of all groups that need heavy preprocessing (coordi + exes)."""
        return {g.jid for g in self.groups if g.is_noisy_group}

    def resolve(self, audience: str) -> list[str]:
        """Map a logical audience to concrete JIDs.
          - a JID (contains '@')          -> itself
          - 'project:<ref>' or a bare ref -> that project's group(s)
          - 'announce' / 'all' / ''       -> announcement group(s)
          - 'events'                      -> events group(s)
          - 'coordi'                      -> coordi group(s)
          - 'exes' / 'executives'         -> exes group(s)
        Falls back to announcement groups when nothing else matches, so an
        output is never silently dropped."""
        if not audience:
            return self.announce_groups()
        if "@" in audience:
            return [audience]
        key = audience.split(":", 1)[-1] if audience.startswith("project:") else audience
        if audience in ("announce", "all", "broadcast"):
            return self.announce_groups()
        if audience == "events":
            return self.events_groups() or self.announce_groups()
        if audience in ("coordi", "faculty"):
            return self.coordi_groups() or self.announce_groups()
        if audience in ("exes", "executives"):
            return self.exes_groups() or self.announce_groups()
        hit = self.groups_for_project(key)
        return hit if hit else self.announce_groups()
