"""
Markdown + YAML frontmatter parse/dump. Same convention as the
innovation-agent's LapisAdapter (--- fenced YAML at the top of a note).

Works with OR without PyYAML: when yaml isn't installed, a lightweight
fallback handles the subset of YAML that our own `dump()` and
`_naive_dump()` produce — simple key: value pairs, inline lists, booleans,
and numbers. Install PyYAML for full YAML support (nested structures,
multi-line strings, etc.).
"""
from __future__ import annotations

import json
import re
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def parse(text: str) -> tuple[dict[str, Any], str]:
    """(frontmatter dict, body). Empty dict if there's no frontmatter."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    raw_meta = parts[1]
    body = parts[2].lstrip("\n")
    if yaml:
        meta = yaml.safe_load(raw_meta) or {}
        if isinstance(meta, dict):
            return meta, body
        return {}, text
    # Fallback: lightweight parser for the subset we emit ourselves
    meta = _naive_parse(raw_meta)
    return meta, body


def dump(meta: dict[str, Any], body: str) -> str:
    """Serialize back to a note string."""
    if not meta:
        return body
    header = yaml.safe_dump(meta, sort_keys=False) if yaml else _naive_dump(meta)
    return f"---\n{header}---\n\n{body.rstrip()}\n"


def _naive_dump(meta: dict[str, Any]) -> str:
    """Serialize frontmatter without PyYAML. Uses JSON for complex values
    (nested dicts, lists of dicts) so _naive_parse can round-trip them."""
    lines = []
    for k, v in meta.items():
        if isinstance(v, dict) or (isinstance(v, list) and v and isinstance(v[0], dict)):
            # Complex value: encode as JSON so we can round-trip it
            lines.append(f"{k}: {json.dumps(v)}")
        elif isinstance(v, list):
            lines.append(f"{k}: [{', '.join(_scalar_str(x) for x in v)}]")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines) + "\n"


def _scalar_str(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _naive_parse(raw: str) -> dict[str, Any]:
    """Parse the simple YAML subset that _naive_dump and yaml.safe_dump produce."""
    meta: dict[str, Any] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        meta[key] = _parse_value(val)
    return meta


def _parse_value(val: str) -> Any:
    """Coerce a frontmatter value string to the right Python type."""
    if not val or val in ("null", "~", "None"):
        return None
    # Boolean
    if val in ("true", "True", "yes"):
        return True
    if val in ("false", "False", "no"):
        return False
    # Quoted string
    if (val.startswith('"') and val.endswith('"')) or \
       (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    # Inline list: [a, b, c]
    if val.startswith("["):
        # Try JSON first (handles nested structures)
        try:
            return json.loads(val)
        except (ValueError, json.JSONDecodeError):
            pass
        # Simple inline list: [a, b, c]
        inner = val[1:].rstrip("]").strip()
        if not inner:
            return []
        return [_parse_value(item.strip()) for item in inner.split(",")]
    # JSON object (our _naive_dump encodes complex values as JSON)
    if val.startswith("{"):
        try:
            return json.loads(val)
        except (ValueError, json.JSONDecodeError):
            return val
    # Integer
    try:
        return int(val)
    except ValueError:
        pass
    # Float
    try:
        return float(val)
    except ValueError:
        pass
    # Plain string (strip trailing quotes if any)
    return val
