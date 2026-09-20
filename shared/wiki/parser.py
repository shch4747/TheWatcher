"""Parser/serialiser for wiki pages (Wiki Format, principles 1-4, 11).

The model stores verbatim slices of the source text (frontmatter block,
preamble, each H2 section's header + body) rather than reformatting
anything. `dump_page(parse_page(text)) == text` holds for *any* input by
construction; callers that want to change a section replace its `.body`
and everything else is untouched byte-for-byte. Rewriting managed/derived
sections wholesale is Phase 1's regeneration job (see `shared/wiki/items.py`
for the line grammar used when reading/writing item content within a
section body).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from shared.wiki.schema import SCHEMA_BY_TYPE, Frontmatter

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?\r?\n)---\r?\n?", re.DOTALL)
_SECTION_HEADER_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*\r?\n", re.MULTILINE)


class WikiParseError(ValueError):
    pass


@dataclass
class Section:
    title: str
    header_raw: str
    body: str = ""

    def full_text(self) -> str:
        return self.header_raw + self.body


@dataclass
class Page:
    page_type: str
    frontmatter: Frontmatter
    frontmatter_raw: str
    preamble: str
    sections: list[Section] = field(default_factory=list)

    def section(self, title: str) -> Section | None:
        from shared.wiki.schema import canonical_section_title

        canonical = canonical_section_title(self.page_type, title)
        for s in self.sections:
            if canonical_section_title(self.page_type, s.title) == canonical:
                return s
        return None


@dataclass
class _Split:
    page_type: str
    raw_frontmatter: dict
    frontmatter_raw: str
    preamble: str
    sections: list[Section]


def _split(text: str) -> _Split:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise WikiParseError("page has no frontmatter block")

    frontmatter_raw = match.group(1)
    raw = yaml.safe_load(frontmatter_raw) or {}
    if "type" not in raw:
        raise WikiParseError("frontmatter missing required 'type' key")

    rest = text[match.end() :]
    header_matches = list(_SECTION_HEADER_RE.finditer(rest))
    preamble = rest[: header_matches[0].start()] if header_matches else rest

    sections: list[Section] = []
    for i, hm in enumerate(header_matches):
        body_start = hm.end()
        body_end = header_matches[i + 1].start() if i + 1 < len(header_matches) else len(rest)
        sections.append(
            Section(title=hm.group(1), header_raw=hm.group(0), body=rest[body_start:body_end])
        )

    return _Split(
        page_type=raw["type"],
        raw_frontmatter=raw,
        frontmatter_raw=frontmatter_raw,
        preamble=preamble,
        sections=sections,
    )


def parse_page(text: str) -> Page:
    """Strict parse: raises WikiParseError/pydantic.ValidationError on a
    bad page. Use `parse_page_lenient` where a bad page must still be
    inspectable (e.g. lint) rather than aborting the whole run."""
    split = _split(text)
    schema_cls = SCHEMA_BY_TYPE.get(split.page_type, Frontmatter)
    frontmatter = schema_cls.model_validate(split.raw_frontmatter)
    return Page(
        page_type=split.page_type,
        frontmatter=frontmatter,
        frontmatter_raw=split.frontmatter_raw,
        preamble=split.preamble,
        sections=split.sections,
    )


def parse_page_lenient(text: str) -> tuple[Page, Exception | None]:
    """Structure (sections, preamble) always parses if the frontmatter
    block exists at all; strict per-type validation failure is returned
    as the second element instead of raised, with `page.frontmatter`
    falling back to the loose base model so section data is still usable
    (Wiki Format principle 12: lint reports, it does not repair - and it
    can't report on sections it never got to look at)."""
    split = _split(text)
    schema_cls = SCHEMA_BY_TYPE.get(split.page_type, Frontmatter)
    error: Exception | None = None
    try:
        frontmatter: Frontmatter = schema_cls.model_validate(split.raw_frontmatter)
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not swallowed
        error = exc
        frontmatter = Frontmatter.model_construct(**split.raw_frontmatter)

    page = Page(
        page_type=split.page_type,
        frontmatter=frontmatter,
        frontmatter_raw=split.frontmatter_raw,
        preamble=split.preamble,
        sections=split.sections,
    )
    return page, error


def dump_page(page: Page) -> str:
    out = f"---\n{page.frontmatter_raw}---\n{page.preamble}"
    for s in page.sections:
        out += s.full_text()
    return out
