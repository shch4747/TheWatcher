"""
Markdown + YAML frontmatter parse/dump. Same convention as the
innovation-agent's LapisAdapter (--- fenced YAML at the top of a note).
"""
from __future__ import annotations

from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def parse(text: str) -> tuple[dict[str, Any], str]:
    """(frontmatter dict, body). Empty dict if there's no frontmatter."""
    if yaml and text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            meta = yaml.safe_load(parts[1]) or {}
            if isinstance(meta, dict):
                return meta, parts[2].lstrip("\n")
    return {}, text


def dump(meta: dict[str, Any], body: str) -> str:
    """Serialize back to a note string."""
    if not meta:
        return body
    header = yaml.safe_dump(meta, sort_keys=False) if yaml else _naive_dump(meta)
    return f"---\n{header}---\n\n{body.rstrip()}\n"


def _naive_dump(meta: dict[str, Any]) -> str:  # pragma: no cover
    lines = []
    for k, v in meta.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(map(str, v))}]")
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines) + "\n"
