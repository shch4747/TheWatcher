"""The page editor (ADR-0008; Wiki Format principles 2-5, 12): the only way
agent code changes a page. Every function takes a parsed `Page` and returns
a new one; none of them trust what they are given to write.

The rules are enforced here, not left to callers:

- **Section Owner.** Writing a section checks its owner in the Page Schema.
  `managed`/`derived` sections are rewritten (`set_fenced`), `append`
  sections only grow (`append_lines`, `append_items`), `shared` sections
  take appended lines, and `human` sections are refused unless the write
  is a confirmed Proposal (`allow_human=True`). A section the schema
  doesn't know is refused outright.
- **Fence.** Inside a fenced section only the region between
  `<!-- watcher:<owner> -->` and `<!-- /watcher -->` is ever replaced;
  bytes a human wrote before or after it survive every write. A section
  whose fence a human deleted gets a new one after their text.
- **Missing sections** are handled the same way by every function: the
  schema's skeleton is inserted at its schema position, then written to.
- **Content can't break structure.** Every line written into a section is
  neutralised first: a markdown heading becomes `\\#...` (so model output
  can never open a new `##` section), fence markers are escaped (so it can
  never close or open a fence). This is what makes a hallucinating model
  unable to corrupt the page format - there is no caller that forgot.
- **Frontmatter is validated.** `set_field` patches one key in place and
  re-validates the whole block against the page type's schema; a value
  the schema rejects (a `state` that isn't a state) raises instead of
  being written.

`new_page_sections` renders a page type's section skeleton from the same
schema, so templates can't drift from what the editor enforces.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from datetime import date, datetime

import yaml
from pydantic import ValidationError

from shared.wiki.items import Item, format_item_line, parse_item_line
from shared.wiki.parser import Page, Section, dump_page, parse_page
from shared.wiki.schema import SECTION_OWNERS, Owner, canonical_section_title, owner_of

FENCE_CLOSE = "<!-- /watcher -->"
_FENCE_OPEN_RE = re.compile(r"<!-- watcher:(\w+) -->\n?")
_HEADING_RE = re.compile(r"^(\s{0,3})(#{1,6})(?=\s|$)")
_FENCE_MARKER_RE = re.compile(r"<!--(\s*/?watcher)")
_H1_RE = re.compile(r"^#[ \t]+.*$", re.MULTILINE)
_PLAIN_SCALAR_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:+\-]*$")

_FENCED_OWNERS: frozenset[str] = frozenset({"managed", "append", "derived"})


class SectionOwnershipError(ValueError):
    """The write isn't allowed on this section by its Section Owner."""


class InvalidPageEdit(ValueError):
    """The edit would leave the page invalid against its schema."""


def fence_open(owner: str) -> str:
    return f"<!-- watcher:{owner} -->"


def neutralise(text: str) -> str:
    """Make arbitrary text safe to put inside a section body: headings are
    escaped (`\\##`, renders as a literal `##`) and fence markers are
    defanged (`&lt;!--`). Everything else is kept exactly."""
    lines = []
    for line in text.split("\n"):
        line = _HEADING_RE.sub(lambda m: f"{m.group(1)}\\{m.group(2)}", line)
        lines.append(_FENCE_MARKER_RE.sub(r"&lt;!--\1", line))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------


def _owner(page: Page, title: str) -> Owner:
    owner = owner_of(page.page_type, title)
    if owner is None:
        raise SectionOwnershipError(f"a {page.page_type} page has no section {title!r} in its schema")
    return owner


def _require(page: Page, title: str, allowed: set[str]) -> Owner:
    owner = _owner(page, title)
    if owner not in allowed:
        raise SectionOwnershipError(
            f"section {title!r} on a {page.page_type} page is owned by {owner!r}; "
            f"this write needs one of {sorted(allowed)}"
        )
    return owner


def _skeleton_body(owner: str, content: str = "", last: bool = False) -> str:
    if owner in _FENCED_OWNERS:
        inner = f"{content}\n" if content else ""
        return f"{fence_open(owner)}\n{inner}{FENCE_CLOSE}\n"
    if content:
        return f"{content}\n" if last else f"{content}\n\n"
    return "" if last else "\n"


def _with_section(page: Page, title: str, owner: str) -> tuple[Page, int]:
    """The page with `title` present (inserted at its schema position if
    missing), and that section's index."""
    canonical = canonical_section_title(page.page_type, title)
    for i, section in enumerate(page.sections):
        if canonical_section_title(page.page_type, section.title) == canonical:
            return page, i

    order = list(SECTION_OWNERS.get(page.page_type, {}))
    later = set(order[order.index(canonical) + 1 :]) if canonical in order else set()
    insert_at = next(
        (
            i
            for i, s in enumerate(page.sections)
            if canonical_section_title(page.page_type, s.title) in later
        ),
        len(page.sections),
    )
    sections = list(page.sections)
    if insert_at > 0 and not sections[insert_at - 1].body.endswith("\n"):
        prev = sections[insert_at - 1]
        sections[insert_at - 1] = Section(prev.title, prev.header_raw, prev.body + "\n")
    elif insert_at == 0 and page.preamble and not page.preamble.endswith("\n"):
        page = _replace(page, preamble=page.preamble + "\n")
    sections.insert(insert_at, Section(canonical, f"## {canonical}\n", _skeleton_body(owner)))
    return _replace(page, sections=sections), insert_at


def _replace(page: Page, **changes) -> Page:
    return Page(
        page_type=changes.get("page_type", page.page_type),
        frontmatter=changes.get("frontmatter", page.frontmatter),
        frontmatter_raw=changes.get("frontmatter_raw", page.frontmatter_raw),
        preamble=changes.get("preamble", page.preamble),
        sections=changes.get("sections", page.sections),
    )


def _set_body(page: Page, index: int, body: str) -> Page:
    sections = list(page.sections)
    old = sections[index]
    sections[index] = Section(old.title, old.header_raw, body)
    return _replace(page, sections=sections)


def _split_fence(body: str, owner: str) -> tuple[str, str, str]:
    """(before, inner, after) around the section's fence. A section with no
    (complete) fence gets one appended after whatever a human left there."""
    open_match = _FENCE_OPEN_RE.search(body)
    close_at = body.find(FENCE_CLOSE, open_match.end()) if open_match else -1
    if open_match is None or close_at < 0:
        before = body if not body or body.endswith("\n") else body + "\n"
        return before + fence_open(owner) + "\n", "", FENCE_CLOSE + "\n"
    return body[: open_match.end()], body[open_match.end() : close_at], body[close_at:]


def _inner(text: str) -> str:
    text = text.strip("\n")
    return f"{text}\n" if text else ""


def fenced_content(page: Page, title: str) -> str:
    """What's inside a section's fence (the agent-written region), or ""."""
    section = page.section(title)
    if section is None:
        return ""
    open_match = _FENCE_OPEN_RE.search(section.body)
    if open_match is None:
        return ""
    close_at = section.body.find(FENCE_CLOSE, open_match.end())
    return section.body[open_match.end() : close_at if close_at >= 0 else None].strip("\n")


def set_fenced(page: Page, title: str, body: str) -> Page:
    """Rewrite a `managed` or `derived` section's fenced region wholesale."""
    owner = _require(page, title, {"managed", "derived"})
    page, index = _with_section(page, title, owner)
    before, _, after = _split_fence(page.sections[index].body, owner)
    return _set_body(page, index, before + _inner(neutralise(body)) + after)


def append_lines(page: Page, title: str, lines: Sequence[str], *, allow_human: bool = False) -> Page:
    """Add lines to an `append` section (inside its fence) or a `shared`
    one (at the end). A `human` section only with `allow_human` - the
    write a confirmed Proposal performs."""
    allowed = {"append", "shared"} | ({"human"} if allow_human else set())
    owner = _require(page, title, allowed)
    if not lines:
        return page
    page, index = _with_section(page, title, owner)
    addition = neutralise("\n".join(line.rstrip("\n") for line in lines))
    body = page.sections[index].body
    if owner == "append":
        before, inner, after = _split_fence(body, owner)
        return _set_body(page, index, before + _inner(inner + "\n" + addition) + after)
    last = index == len(page.sections) - 1
    kept = body.rstrip("\n")
    new_body = f"{kept}\n{addition}\n" if kept else f"{addition}\n"
    return _set_body(page, index, new_body if last else new_body + "\n")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def append_items(page: Page, title: str, items: Sequence[Item]) -> Page:
    """Append Items to an `append` section, skipping any already there (same
    block id, or the same text) - so applying the same Items twice leaves
    the page unchanged."""
    _require(page, title, {"append"})
    section = page.section(title)
    present = [parse_item_line(line) for line in section.body.splitlines()] if section else []
    ids = {i.block_id for i in present if i}
    texts = {_norm(i.text) for i in present if i}
    new_lines = []
    for item in items:
        if item.block_id in ids or _norm(item.text) in texts:
            continue
        ids.add(item.block_id)
        texts.add(_norm(item.text))
        new_lines.append(format_item_line(item))
    return append_lines(page, title, new_lines) if new_lines else page


Merge = Callable[[Item, Item], Item]


def upsert_items(page: Page, title: str, items: Sequence[Item], merge: Merge | None = None) -> Page:
    """Insert-or-update Items in a `managed` section by block id. An
    existing line is replaced in place (`merge(existing, incoming)`
    decides what survives - by default the incoming item); every other
    line inside the fence, prose included, is kept verbatim; new Items are
    appended."""
    owner = _require(page, title, {"managed"})
    page, index = _with_section(page, title, owner)
    before, inner, after = _split_fence(page.sections[index].body, owner)
    incoming = {item.block_id: item for item in items}
    out: list[str] = []
    replaced: set[str] = set()
    for line in inner.splitlines():
        existing = parse_item_line(line.rstrip())
        if existing is not None and existing.block_id in incoming and existing.block_id not in replaced:
            new = incoming[existing.block_id]
            out.append(format_item_line(merge(existing, new) if merge else new))
            replaced.add(existing.block_id)
        else:
            out.append(line)
    out += [format_item_line(i) for i in items if i.block_id not in replaced]
    return _set_body(page, index, before + _inner(neutralise("\n".join(out))) + after)


# --------------------------------------------------------------------------
# frontmatter + title
# --------------------------------------------------------------------------


def yaml_str(value: str) -> str:
    """Free text as a YAML scalar that can't corrupt the frontmatter
    block: a JSON string literal is always a valid YAML double-quoted
    scalar, so embedded newlines, quotes, colons or a leading `-`/`#`/`[`
    stay inside their own field."""
    return json.dumps(value, ensure_ascii=False)


class Raw:
    """A frontmatter value already in YAML form (e.g. `"[[Member]]"`),
    written as given - still re-validated by `set_field`."""

    def __init__(self, text: str):
        self.text = text


def yaml_value(value: object) -> str:
    """A Python value as a frontmatter scalar. Short plain tokens (states,
    ids, timestamps) are written bare, as a human would; anything else is
    quoted."""
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Raw):
        return value.text
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(yaml_str(str(v)) for v in value) + "]"
    text = str(value)
    if _PLAIN_SCALAR_RE.match(text) and yaml.safe_load(text) == text:
        return text  # reads back as this exact string, so no quotes needed
    return yaml_str(text)


def set_field(page: Page, key: str, value: object) -> Page:
    """Set one frontmatter key, leaving every other byte of the block (key
    order, unknown keys, a human's formatting) as it was, then re-validate
    against the page type's schema. Raises `InvalidPageEdit` for a value
    the schema rejects."""
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    line = f"{key}: {yaml_value(value)}".rstrip()
    if pattern.search(page.frontmatter_raw):
        raw = pattern.sub(lambda _: line, page.frontmatter_raw, count=1)
    else:
        raw = page.frontmatter_raw.rstrip("\n") + f"\n{line}\n"
    candidate = _replace(page, frontmatter_raw=raw)
    try:
        validated = parse_page(dump_page(candidate))
    except (ValidationError, ValueError) as exc:
        raise InvalidPageEdit(f"{key}={value!r} is not valid on a {page.page_type} page: {exc}") from exc
    return _replace(candidate, frontmatter=validated.frontmatter)


def set_title(page: Page, title: str) -> Page:
    """Frontmatter `title:` and the `# Title` heading, kept in step. The
    title is collapsed to one line - it's metadata, not prose."""
    title = re.sub(r"\s+", " ", title).strip()
    page = set_field(page, "title", title)
    if _H1_RE.search(page.preamble):
        preamble = _H1_RE.sub(lambda _: f"# {title}", page.preamble, count=1)
    else:
        preamble = f"# {title}\n" + page.preamble
    return _replace(page, preamble=preamble)


# --------------------------------------------------------------------------
# new pages
# --------------------------------------------------------------------------


def new_page_sections(page_type: str, content: dict[str, str] | None = None) -> str:
    """Every section of `page_type`, in schema order: fenced skeletons for
    agent-owned sections, empty bodies for human/shared ones, each filled
    with `content[title]` if given (neutralised like any other write)."""
    content = content or {}
    owners = SECTION_OWNERS[page_type]
    unknown = set(content) - set(owners)
    if unknown:
        raise SectionOwnershipError(f"a {page_type} page has no sections {sorted(unknown)}")
    titles = list(owners)
    out = []
    for i, title in enumerate(titles):
        body = neutralise(content.get(title, "").strip("\n"))
        out.append(f"## {title}\n" + _skeleton_body(owners[title], body, last=i == len(titles) - 1))
    return "".join(out)
