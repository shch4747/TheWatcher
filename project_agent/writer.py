"""
Writes agent-computed state back to a project's Lapis page.

Frontmatter is overwritten wholesale (it's fully agent-owned and
structured, so this is safe and easy to diff).

Body sections are updated in place via a marker-based replace, so
human-written prose (Goal, Milestones) is never touched even though the
agent rewrites the file. Sections not yet present are appended in a
fixed order the first time they're needed.
"""

from __future__ import annotations

import re
from typing import Any

from lapis_client import LapisNote

SECTION_ORDER = ["Goal", "Current status", "Milestones", "Healthbar", "Post-mortem", "Similarity notes"]

_SECTION_RE_TEMPLATE = r"(^## {name}\n)(.*?)(?=^## |\Z)"


def _get_section(body: str, name: str) -> str | None:
    m = re.search(_SECTION_RE_TEMPLATE.format(name=re.escape(name)), body, re.MULTILINE | re.DOTALL)
    return m.group(2).strip() if m else None


def _set_section(body: str, name: str, content: str) -> str:
    pattern = re.compile(_SECTION_RE_TEMPLATE.format(name=re.escape(name)), re.MULTILINE | re.DOTALL)
    replacement = f"## {name}\n{content.strip()}\n\n"
    if pattern.search(body):
        return pattern.sub(lambda m: replacement, body)
    # Section doesn't exist yet -- append in canonical order.
    return body.rstrip() + f"\n\n{replacement}"


def update_current_status(body: str, summary_line: str) -> str:
    return _set_section(body, "Current status", summary_line)


def update_healthbar_section(body: str, score: int, trend: str, mermaid_chart: str | None) -> str:
    trend_symbol = {"up": "▲", "down": "▼", "flat": "→"}.get(trend, "")
    content = f"**Score:** {score}/100 {trend_symbol}\n"
    if mermaid_chart:
        content += f"\n```mermaid\n{mermaid_chart}\n```\n"
    return _set_section(body, "Healthbar", content)


def append_post_mortem(body: str, post_mortem_text: str) -> str:
    # Only ever written once (on confirmed death) -- if a Post-mortem
    # section already exists, leave it alone rather than overwrite.
    if _get_section(body, "Post-mortem"):
        return body
    return _set_section(body, "Post-mortem", post_mortem_text)


def update_similarity_notes(body: str, flags: list[dict[str, Any]]) -> str:
    if not flags:
        return body
    lines = [
        f"- Possible overlap with `{f['project']}` (similarity {f['score']:.2f}, status: {f['status']})"
        for f in flags
    ]
    return _set_section(body, "Similarity notes", "\n".join(lines))


def build_mermaid_healthbar_chart(history: list[int], labels: list[str] | None = None) -> str:
    labels = labels or [f"t-{len(history) - i - 1}" for i in range(len(history))]
    label_str = ", ".join(labels)
    values_str = ", ".join(str(v) for v in history)
    return (
        "xychart-beta\n"
        '    title "Healthbar history"\n'
        f"    x-axis [{label_str}]\n"
        '    y-axis "Score" 0 --> 100\n'
        f"    line [{values_str}]"
    )


def write_note(note: LapisNote, client) -> None:
    """Small wrapper so callers don't have to import lapis_client directly
    just to persist a note -- keeps main.py's imports minimal."""
    client.write_note(note)
