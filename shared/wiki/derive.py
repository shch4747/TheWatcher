"""Derived pages (Wiki Format principles 3, 8): the root indexes and the
README dashboard, rendered whole from frontmatter. Derived *sections* on
other pages are written through `shared.wiki.editor.set_fenced`, which
refuses any section whose owner isn't `derived` or `managed`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class MemberIndexRow:
    title: str
    role: str
    status: str


def render_members_index(members: list[MemberIndexRow]) -> str:
    """`Members.md` body - a derived index generated from frontmatter."""
    lines = ["# Members", "", "| Member | Role | Status |", "| --- | --- | --- |"]
    for m in sorted(members, key=lambda r: r.title.lower()):
        lines.append(f"| [[{m.title}]] | {m.role} | {m.status} |")
    return "\n".join(lines) + "\n"


@dataclass
class ReadmeData:
    active_initiatives: list[str]
    tasks_due: list[str]
    new_threads: list[str]
    upcoming_events: list[str]


def render_readme(data: ReadmeData, as_of: date | None = None) -> str:
    """`README.md` dashboard (User story 29: "this week across ARIES")."""
    as_of = as_of or date.today()
    lines = [f"# ARIES this week ({as_of.isoformat()})", ""]

    def _section(title: str, rows: list[str]) -> None:
        lines.append(f"## {title}")
        if rows:
            lines.extend(f"- {row}" for row in rows)
        else:
            lines.append("- (none)")
        lines.append("")

    _section("Active initiatives", data.active_initiatives)
    _section("Tasks due", data.tasks_due)
    _section("New threads", data.new_threads)
    _section("Upcoming events", data.upcoming_events)
    return "\n".join(lines).rstrip() + "\n"
