"""Skill loading (Spec: "Skills" - SKILL.md folders, loaded from the
repo's `skills/` and the wiki's `meta/skills/`; wiki wins on name
collision). Skills are prompt/instruction bundles for Worker/Mentor
agents, not code - this module only reads and parses them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?\r?\n)---\r?\n?", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    source: str  # "repo" | "wiki"


def parse_skill_md(text: str, source: str) -> Skill:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("SKILL.md has no frontmatter block")
    meta = yaml.safe_load(match.group(1)) or {}
    if "name" not in meta:
        raise ValueError("SKILL.md frontmatter missing required 'name' key")
    body = text[match.end() :]
    return Skill(
        name=meta["name"], description=meta.get("description", ""), instructions=body.strip(), source=source
    )


def load_skills_from_dir(root: Path, source: str) -> dict[str, Skill]:
    """Each skill lives at `<root>/<skill-name>/SKILL.md`."""
    skills: dict[str, Skill] = {}
    if not root.exists():
        return skills
    for skill_file in sorted(root.glob("*/SKILL.md")):
        skill = parse_skill_md(skill_file.read_text(), source=source)
        skills[skill.name] = skill
    return skills


def load_skills(repo_skills_dir: Path, wiki_skills_dir: Path | None = None) -> dict[str, Skill]:
    """`repo_skills_dir` loaded first, then `wiki_skills_dir` - a name
    present in both is taken from the wiki (Spec: "wiki wins on name
    collision"), so behaviour is tunable without a deploy."""
    skills = load_skills_from_dir(repo_skills_dir, source="repo")
    if wiki_skills_dir is not None:
        skills.update(load_skills_from_dir(wiki_skills_dir, source="wiki"))
    return skills
