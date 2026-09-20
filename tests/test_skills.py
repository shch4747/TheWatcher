"""Phase 4 skills-loader acceptance criteria (docs/Plan - Watcher v1.md):
a skill defined only in meta/skills/ overrides a same-named repo skill;
the three named skills exist; adding a skill needs no code change."""
from __future__ import annotations

from pathlib import Path

from shared.models.interface import load_skills, load_skills_from_dir, parse_skill_md

REPO_SKILLS = Path(__file__).parent.parent / "skills"


def test_the_three_named_skills_exist_and_load():
    skills = load_skills_from_dir(REPO_SKILLS, source="repo")
    assert set(skills) >= {"summarise-thread", "extract-items", "name-thread"}
    for skill in skills.values():
        assert skill.description
        assert skill.instructions
        assert skill.source == "repo"


def test_parse_skill_md_requires_name():
    try:
        parse_skill_md("---\ndescription: no name here\n---\nbody", source="repo")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a SKILL.md missing 'name'")


def test_wiki_skill_overrides_same_named_repo_skill(tmp_path: Path):
    wiki_skills = tmp_path / "meta" / "skills"
    override_dir = wiki_skills / "summarise-thread"
    override_dir.mkdir(parents=True)
    (override_dir / "SKILL.md").write_text(
        "---\nname: summarise-thread\ndescription: tuned from the wiki\n---\nWiki-tuned instructions.\n"
    )

    skills = load_skills(REPO_SKILLS, wiki_skills)
    assert skills["summarise-thread"].source == "wiki"
    assert skills["summarise-thread"].description == "tuned from the wiki"
    # a skill that only exists in the repo is untouched
    assert skills["extract-items"].source == "repo"


def test_missing_wiki_skills_dir_is_not_an_error(tmp_path: Path):
    skills = load_skills(REPO_SKILLS, tmp_path / "does-not-exist")
    assert "name-thread" in skills
