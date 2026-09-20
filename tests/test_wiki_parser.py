"""Phase 1 wiki layer: schema validation, round-trip, item grammar
(docs/Wiki Format.md, docs/Spec - Watcher v1.md "Wiki layer")."""
from __future__ import annotations

from pathlib import Path

import pytest
from shared.wiki.interface import (
    canonical_section_title,
    dump_page,
    owner_of,
    parse_items,
    parse_page,
)

FIXTURES = Path(__file__).parent / "fixtures" / "wiki"


@pytest.mark.parametrize(
    "filename",
    ["member_aira.md", "project_watcher.md", "thread_demo_date.md", "project_handedit.md"],
)
def test_round_trip_is_byte_identical(filename: str):
    original = (FIXTURES / filename).read_text()
    page = parse_page(original)
    assert dump_page(page) == original


def test_round_trip_twice_is_stable(filename: Path = FIXTURES / "project_watcher.md"):
    original = filename.read_text()
    once = dump_page(parse_page(original))
    twice = dump_page(parse_page(once))
    assert once == twice == original


def test_member_frontmatter_validates_known_fields():
    page = parse_page((FIXTURES / "member_aira.md").read_text())
    assert page.frontmatter.type == "member"
    assert page.frontmatter.role == "Executive"
    assert page.frontmatter.status == "active"


def test_project_frontmatter_preserves_unknown_keys():
    text = (FIXTURES / "project_watcher.md").read_text().replace(
        "category: Internal", "category: Internal\nunknown_future_key: surprise"
    )
    page = parse_page(text)
    assert page.frontmatter.model_extra is not None
    assert page.frontmatter.model_extra.get("unknown_future_key") == "surprise"
    # round trip is still exact even though the extra key exists, since we
    # never re-serialize frontmatter ourselves in Phase 1.
    assert dump_page(page) == text


def test_unknown_section_is_preserved_verbatim():
    page = parse_page((FIXTURES / "project_handedit.md").read_text())
    titles = [s.title for s in page.sections]
    assert "Custom Section Nobody Told The Parser About" in titles


def test_section_lookup_by_alias():
    page = parse_page((FIXTURES / "project_handedit.md").read_text())
    assert page.section("Open tasks") is not None
    assert page.section("Tasks") is page.section("Open tasks")


def test_canonical_section_title_and_owner():
    assert canonical_section_title("project", "Tasks") == "Open tasks"
    assert owner_of("project", "Open tasks") == "managed"
    assert owner_of("project", "Brief") == "human"
    assert owner_of("project", "Nonexistent Section") is None


def test_parse_items_extracts_task_decision_resource_question():
    page = parse_page((FIXTURES / "thread_demo_date.md").read_text())
    items = parse_items(page.section("Items").body)
    assert len(items) == 4

    task = items[0]
    assert task.checked is False
    assert task.fields["kind"] == "task"
    assert task.fields["owner"] == "[[Aira]]"
    assert task.block_id == "i-9f3c"

    decision = items[1]
    assert decision.checked is True
    assert decision.src_ids() == ["3EB0C1", "3EB0C2"]

    resource = items[2]
    assert resource.checked is None
    assert resource.text.startswith("https://example.com/slides")

    question = items[3]
    assert question.fields["by"] == "[[Devansh]]"


def test_hand_reworded_item_still_parses_by_block_id():
    page = parse_page((FIXTURES / "project_handedit.md").read_text())
    items = parse_items(page.section("Open tasks").body)
    assert len(items) == 1
    assert items[0].block_id == "t-9f3c"
    assert items[0].checked is True
    assert "Reworded by a human" in items[0].text


def test_bare_bullet_without_block_id_is_not_an_item():
    page = parse_page((FIXTURES / "project_handedit.md").read_text())
    items = parse_items(page.section("Notes").body)
    assert items == []
