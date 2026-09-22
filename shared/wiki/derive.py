"""Derived regeneration (Wiki Format principles 3, 8): regeneration
touches *only* sections/pages whose owner is `derived`. Member
Projects/Past projects/Open tasks, initiative Threads, channel indexes
and root indexes all come from this module; hand edits to a derived
section are lost and lint warns (Phase 1's lint job flags it separately).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from shared.wiki.parser import Page, Section
from shared.wiki.schema import canonical_section_title, owner_of

_FIELD_LINE_RE_TEMPLATE = r"^{key}:.*$"


class NotDerivedError(ValueError):
    """Raised when code tries to regenerate a section a human or another
    writer owns - regeneration must never touch those (Wiki Format,
    principle 3)."""


def set_derived_section(page: Page, title: str, new_body: str) -> Page:
    """Wholesale rewrite of a derived section's fenced content - same
    fence convention as `replace_managed_section` (`<!-- watcher:derived
    -->`), so lint and a human reading the raw file can tell at a glance
    this section is machine-owned, same as managed/append ones."""
    owner = owner_of(page.page_type, title)
    if owner != "derived":
        raise NotDerivedError(
            f"section {title!r} on a {page.page_type} page is owned by {owner!r}, not derived"
        )

    fenced_body = f"<!-- watcher:derived -->\n{new_body}\n<!-- /watcher -->\n"
    target_canonical = canonical_section_title(page.page_type, title)
    new_sections = []
    replaced = False
    for section in page.sections:
        if canonical_section_title(page.page_type, section.title) == target_canonical:
            new_sections.append(
                Section(title=section.title, header_raw=section.header_raw, body=fenced_body)
            )
            replaced = True
        else:
            new_sections.append(section)
    if not replaced:
        new_sections.append(Section(title=title, header_raw=f"## {title}\n", body=fenced_body))

    return Page(
        page_type=page.page_type,
        frontmatter=page.frontmatter,
        frontmatter_raw=page.frontmatter_raw,
        preamble=page.preamble,
        sections=new_sections,
    )


_H1_RE = re.compile(r"^#[ \t]+.*$", re.MULTILINE)


def set_h1_title(page: Page, new_title: str) -> Page:
    """Replace the page's `# Title` heading (the preamble line before
    the first `##` section) - used when a title is regenerated for an
    existing page (e.g. a thread retitled on a later batch), so the
    heading a human actually reads never drifts from `frontmatter.title`."""
    if _H1_RE.search(page.preamble):
        new_preamble = _H1_RE.sub(f"# {new_title}", page.preamble, count=1)
    else:
        new_preamble = f"# {new_title}\n" + page.preamble
    return Page(
        page_type=page.page_type,
        frontmatter=page.frontmatter,
        frontmatter_raw=page.frontmatter_raw,
        preamble=new_preamble,
        sections=page.sections,
    )


def set_frontmatter_field(page: Page, key: str, value: str) -> Page:
    """Patch one frontmatter key in place, leaving every other byte of
    the frontmatter block (including unknown keys, Wiki Format principle
    1) exactly as it was. Used for lifecycle transitions (`state:`,
    `cursor:`) where rewriting the whole block via YAML dump would risk
    reordering or reformatting keys a human wrote by hand."""
    pattern = re.compile(_FIELD_LINE_RE_TEMPLATE.format(key=re.escape(key)), re.MULTILINE)
    new_line = f"{key}: {value}"
    if pattern.search(page.frontmatter_raw):
        new_raw = pattern.sub(new_line, page.frontmatter_raw, count=1)
    else:
        new_raw = page.frontmatter_raw.rstrip("\n") + f"\n{new_line}\n"

    return Page(
        page_type=page.page_type,
        frontmatter=page.frontmatter,
        frontmatter_raw=new_raw,
        preamble=page.preamble,
        sections=page.sections,
    )


def replace_managed_section(page: Page, title: str, new_body: str) -> Page:
    """Wholesale rewrite of a managed section's fenced content, keeping
    the fence markers (Wiki Format: managed = agent rewrites wholesale)."""
    canonical = canonical_section_title(page.page_type, title)
    new_sections = []
    for section in page.sections:
        if canonical_section_title(page.page_type, section.title) == canonical:
            body = f"<!-- watcher:managed -->\n{new_body}\n<!-- /watcher -->\n"
            new_sections.append(Section(title=section.title, header_raw=section.header_raw, body=body))
        else:
            new_sections.append(section)
    return Page(
        page_type=page.page_type,
        frontmatter=page.frontmatter,
        frontmatter_raw=page.frontmatter_raw,
        preamble=page.preamble,
        sections=new_sections,
    )


def append_to_section(page: Page, title: str, lines: list[str]) -> Page:
    """Append new lines to a section - works whether or not the section
    is fenced (append/derived sections are; human/shared ones, like
    Notes, usually aren't). Fenced: inserts just before the closing
    fence. Unfenced: appends at the end of the body."""
    canonical = canonical_section_title(page.page_type, title)
    new_sections = []
    addition = "\n".join(lines)
    for section in page.sections:
        if canonical_section_title(page.page_type, section.title) == canonical:
            body = section.body
            if "<!-- /watcher -->" in body:
                body = body.replace("<!-- /watcher -->", f"{addition}\n<!-- /watcher -->", 1)
            else:
                body = f"{body.rstrip()}\n{addition}\n"
            new_sections.append(Section(title=section.title, header_raw=section.header_raw, body=body))
        else:
            new_sections.append(section)
    return Page(
        page_type=page.page_type,
        frontmatter=page.frontmatter,
        frontmatter_raw=page.frontmatter_raw,
        preamble=page.preamble,
        sections=new_sections,
    )


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
