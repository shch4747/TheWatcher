"""Wiki interface (ADR-0002, ADR-0003, ADR-0008).

Phase 1: schema + parser/serialiser, the Lapis client (live + local-dir
test adapter, read/write with base revision, conflict detection), and
the lint + derived-regeneration jobs. Everything below is re-exported
from this module - nothing outside `shared/wiki/` should import a
submodule directly (ADR-0003).
"""
from __future__ import annotations

from shared.wiki.derive import (
    MemberIndexRow,
    NotDerivedError,
    ReadmeData,
    render_members_index,
    render_readme,
    set_derived_section,
)
from shared.wiki.items import Item, format_item_line, parse_item_line, parse_items
from shared.wiki.lapis_client import (
    ConflictError,
    LapisClient,
    LocalDirClient,
    ReadResult,
    VaultClient,
    WriteResult,
)
from shared.wiki.lint import LintIssue, lint_text, render_lint_report, repair_missing_sections
from shared.wiki.parser import Page, Section, WikiParseError, dump_page, parse_page, parse_page_lenient
from shared.wiki.schema import SCHEMA_BY_TYPE, Frontmatter, canonical_section_title, owner_of
from shared.wiki.templates import (
    render_new_channel_page,
    render_new_event_page,
    render_new_project_page,
    slugify,
)

__all__ = [
    "Item",
    "format_item_line",
    "parse_item_line",
    "parse_items",
    "Page",
    "Section",
    "WikiParseError",
    "dump_page",
    "parse_page",
    "parse_page_lenient",
    "SCHEMA_BY_TYPE",
    "Frontmatter",
    "canonical_section_title",
    "owner_of",
    "ConflictError",
    "LapisClient",
    "LocalDirClient",
    "ReadResult",
    "VaultClient",
    "WriteResult",
    "LintIssue",
    "lint_text",
    "render_lint_report",
    "repair_missing_sections",
    "MemberIndexRow",
    "NotDerivedError",
    "ReadmeData",
    "render_members_index",
    "render_readme",
    "set_derived_section",
    "render_new_channel_page",
    "render_new_event_page",
    "render_new_project_page",
    "slugify",
]
