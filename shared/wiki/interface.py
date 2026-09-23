"""Wiki interface (ADR-0002, ADR-0003, ADR-0008).

Every agent change to a page goes through the editor (`set_fenced`,
`append_lines`, `append_items`, `upsert_items`, `set_field`,
`set_title`), which enforces Section Owners and Fences - there is no
other exported way to modify a section.

Phase 1: schema + parser/serialiser, the Lapis client (live + local-dir
test adapter, read/write with base revision, conflict detection), and
the lint + derived-regeneration jobs. Everything below is re-exported
from this module - nothing outside `shared/wiki/` should import a
submodule directly (ADR-0003).
"""
from __future__ import annotations

from shared.wiki.derive import MemberIndexRow, ReadmeData, render_members_index, render_readme
from shared.wiki.editor import (
    InvalidPageEdit,
    Raw,
    SectionOwnershipError,
    append_items,
    append_lines,
    fenced_content,
    neutralise,
    new_page_sections,
    set_fenced,
    set_field,
    set_title,
    upsert_items,
    yaml_str,
)
from shared.wiki.items import Item, format_item_line, parse_item_line, parse_items
from shared.wiki.lapis_client import (
    ConflictError,
    LapisClient,
    LocalDirClient,
    McpToolError,
    PageExists,
    PageNotFound,
    ReadResult,
    VaultClient,
    WriteResult,
    check_vault_connection,
    default_vault_client,
    read_if_exists,
)
from shared.wiki.lint import (
    LintIssue,
    contains_pii,
    lint_text,
    redact_pii,
    render_lint_report,
    repair_missing_sections,
)
from shared.wiki.parser import Page, Section, WikiParseError, dump_page, parse_page, parse_page_lenient
from shared.wiki.schema import SCHEMA_BY_TYPE, Frontmatter, canonical_section_title, owner_of
from shared.wiki.templates import (
    render_new_channel_page,
    render_new_event_page,
    render_new_member_page,
    render_new_project_page,
    render_new_thread_page,
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
    "McpToolError",
    "PageExists",
    "PageNotFound",
    "read_if_exists",
    "ReadResult",
    "VaultClient",
    "WriteResult",
    "check_vault_connection",
    "default_vault_client",
    "LintIssue",
    "contains_pii",
    "redact_pii",
    "lint_text",
    "render_lint_report",
    "repair_missing_sections",
    "MemberIndexRow",
    "ReadmeData",
    "render_members_index",
    "render_readme",
    "InvalidPageEdit",
    "Raw",
    "SectionOwnershipError",
    "append_items",
    "append_lines",
    "fenced_content",
    "neutralise",
    "new_page_sections",
    "set_fenced",
    "set_field",
    "set_title",
    "upsert_items",
    "render_new_channel_page",
    "render_new_event_page",
    "render_new_member_page",
    "render_new_project_page",
    "render_new_thread_page",
    "slugify",
    "yaml_str",
]
