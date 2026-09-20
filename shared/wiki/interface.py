"""Wiki interface (ADR-0002, ADR-0003, ADR-0008).

Phase 1 slice: schema + parser/serialiser (this module re-exports them).
The Lapis client (read/write with base revision, conflict detection) and
the lint/derived-regeneration jobs are separate Phase 1 tickets — not
implemented here yet.
"""
from __future__ import annotations

from shared.wiki.items import Item, format_item_line, parse_item_line, parse_items
from shared.wiki.lapis_client import (
    ConflictError,
    LapisClient,
    LocalDirClient,
    ReadResult,
    VaultClient,
    WriteResult,
)
from shared.wiki.parser import Page, Section, WikiParseError, dump_page, parse_page
from shared.wiki.schema import SCHEMA_BY_TYPE, Frontmatter, canonical_section_title, owner_of

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
]
