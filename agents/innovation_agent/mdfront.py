"""
mdfront.py — minimal YAML-frontmatter parser/dumper for markdown
files that have structured metadata up top and prose below:

    ---
    key: value
    list:
      - a
      - b
    ---
    # Body content here

Needed by lapis_feedback_store.py (and, eventually, any real WA
output code that uses the same file convention).
"""
import yaml


def parse(raw: str) -> tuple[dict, str]:
    """Returns (meta, body). If there's no frontmatter, meta is {}
    and body is the whole raw string."""
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    _, frontmatter, body = parts
    meta = yaml.safe_load(frontmatter) or {}
    return meta, body.lstrip("\n")


def dump(meta: dict, body: str) -> str:
    """Reassembles meta + body back into the same frontmatter format."""
    frontmatter = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
    return f"---\n{frontmatter}---\n{body}"