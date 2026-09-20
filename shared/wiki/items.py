"""Item line grammar (Wiki Format, principle 5):
`- [ ] text [key:: value] [key2:: value2] ^block-id`
A leading `[x]`/`[ ]` is optional (only task-like items use it); block id is
required so identity survives a reword. Read-only helpers for now — writing
managed sections is Phase 4/5's job, once there's real content to write.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# A field value may itself contain a wikilink ([[Page]]), so it isn't
# simply "anything but ]" - that would truncate at the wikilink's first
# closing bracket. Allow [[...]] runs or any non-bracket char.
_VALUE = r"(?:\[\[.*?\]\]|[^\[\]])*"
_ITEM_LINE_RE = re.compile(
    r"^-\s+(?:\[(?P<checked>[ xX])\]\s+)?(?P<text>.*?)\s*"
    rf"(?P<fields>(?:\[\w+::\s*{_VALUE}\]\s*)*)"
    r"\^(?P<block_id>[a-zA-Z0-9_-]+)\s*$"
)
_FIELD_RE = re.compile(rf"\[(?P<key>\w+)::\s*(?P<value>{_VALUE})\]")


@dataclass
class Item:
    text: str
    block_id: str
    checked: bool | None = None
    fields: dict[str, str] = field(default_factory=dict)

    def src_ids(self) -> list[str]:
        """`[src:: id1, id2]` -> ["id1", "id2"] (Wiki Format, principle 6)."""
        raw = self.fields.get("src", "")
        return [s.strip() for s in raw.split(",") if s.strip()]


def parse_item_line(line: str) -> Item | None:
    """Returns None for a line that isn't item-shaped (prose, blank, etc.) —
    per Wiki Format principle 4, non-matching text is preserved, not flagged
    as an error here (lint's job, Phase 1's other ticket)."""
    m = _ITEM_LINE_RE.match(line)
    if not m:
        return None
    fields = {fm.group("key"): fm.group("value").strip() for fm in _FIELD_RE.finditer(m.group("fields"))}
    checked_raw = m.group("checked")
    return Item(
        text=m.group("text"),
        block_id=m.group("block_id"),
        checked=(checked_raw.lower() == "x") if checked_raw is not None else None,
        fields=fields,
    )


def parse_items(section_body: str) -> list[Item]:
    items = []
    for line in section_body.splitlines():
        item = parse_item_line(line)
        if item is not None:
            items.append(item)
    return items


def format_item_line(item: Item) -> str:
    prefix = "- "
    if item.checked is not None:
        prefix += f"[{'x' if item.checked else ' '}] "
    fields_str = "".join(f" [{k}:: {v}]" for k, v in item.fields.items())
    return f"{prefix}{item.text}{fields_str} ^{item.block_id}"
