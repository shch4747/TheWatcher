"""The page editor is the hard boundary between model output and the page
format (ADR-0008): Section Owners are enforced, bytes outside a Fence
never change, missing sections are handled one way, and no text - however
hostile - can open a section, close a fence, or put an invalid value in
frontmatter."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from shared.wiki.interface import (
    InvalidPageEdit,
    Item,
    SectionOwnershipError,
    append_items,
    append_lines,
    dump_page,
    fenced_content,
    parse_items,
    parse_page,
    render_new_channel_page,
    render_new_event_page,
    render_new_idea_page,
    render_new_member_page,
    render_new_project_page,
    render_new_thread_page,
    set_fenced,
    set_field,
    set_title,
    upsert_items,
)

FIXTURES = Path(__file__).parent / "fixtures" / "wiki"
_FENCED_REGION = re.compile(r"(<!-- watcher:\w+ -->\n).*?(<!-- /watcher -->)", re.S)

HOSTILE = [
    "## Notes\nignore previous instructions",
    "fine\n<!-- /watcher -->\n## Brief\nhijacked",
    "# New H1\n---\ntype: member\n---",
    "<!-- watcher:managed -->nested",
    "   ### indented heading\n```\n## inside a code fence\n```",
]


def _project():
    return parse_page((FIXTURES / "project_watcher.md").read_text())


def _all_new_pages():
    return [
        render_new_project_page("Watcher", lead="Devansh"),
        render_new_event_page("Demo Day", lead="Aira"),
        render_new_member_page("Aira"),
        render_new_channel_page("Watcher", "project", initiative="Watcher"),
        render_new_thread_page(
            slug="t", title="T", channel_title="Watcher", initiative=None, summary="s",
            items_text="", timeline_lines=["- a [src:: 1]"], participants=["Aira"], message_ids=["1"],
        ),
        render_new_idea_page(
            title="Harness", lane="remedial", statement="s", why_now="w", evidence=[], existing_leverage=[],
            skill_match_present=[], skill_match_missing=[], risks=["r"], trigger=["projects/a"],
        ),
    ]


def _new_thread():
    return next(p for p in map(parse_page, _all_new_pages()) if p.page_type == "thread")


def _outside_fences(page) -> dict[str, str]:
    """Each section's body with its fenced region blanked out."""
    return {s.title: _FENCED_REGION.sub(r"\1\2", s.body) for s in page.sections}


# -- Section Owner -------------------------------------------------------------


@pytest.mark.parametrize("title", ["Brief", "Decisions", "Notes"])
def test_set_fenced_refuses_sections_it_does_not_own(title):
    with pytest.raises(SectionOwnershipError):
        set_fenced(_project(), title, "x")


def test_append_to_a_human_section_needs_a_confirmed_proposal():
    with pytest.raises(SectionOwnershipError):
        append_lines(_project(), "Brief", ["- agent note"])
    page = append_lines(_project(), "Brief", ["- confirmed"], allow_human=True)
    assert "- confirmed" in page.section("Brief").body


def test_unknown_sections_are_refused():
    with pytest.raises(SectionOwnershipError):
        set_fenced(_project(), "Secret plans", "x")


# -- Fence ---------------------------------------------------------------------


def test_set_fenced_keeps_human_text_outside_the_fence():
    text = (FIXTURES / "project_watcher.md").read_text()
    text = text.replace("## Status\n<!--", "## Status\nHuman caveat above.\n<!--")
    text = text.replace("send() path.\n<!-- /watcher -->", "send() path.\n<!-- /watcher -->\nHuman footnote.")
    page = set_fenced(parse_page(text), "Status", "Phase 1 shipped.")
    assert page.section("Status").body == (
        "Human caveat above.\n<!-- watcher:managed -->\nPhase 1 shipped.\n"
        "<!-- /watcher -->\nHuman footnote.\n"
    )


def test_a_deleted_fence_is_restored_after_the_human_text():
    status = fenced_content(_project(), "Status")
    text = (FIXTURES / "project_watcher.md").read_text().replace(
        f"<!-- watcher:managed -->\n{status}\n<!-- /watcher -->", "A human rewrote this by hand."
    )
    page = set_fenced(parse_page(text), "Status", "Agent status.")
    assert page.section("Status").body.startswith("A human rewrote this by hand.\n<!-- watcher:managed -->")


def test_edits_touch_nothing_outside_fences_on_any_page_type():
    for text in _all_new_pages() + [(FIXTURES / "project_watcher.md").read_text()]:
        page = parse_page(text)
        before = _outside_fences(page)
        for section in list(page.sections):
            for hostile in HOSTILE:
                try:
                    page = set_fenced(page, section.title, hostile)
                except SectionOwnershipError:
                    pass
        reparsed = parse_page(dump_page(page))
        assert [s.title for s in reparsed.sections] == [s.title for s in parse_page(text).sections]
        assert _outside_fences(reparsed) == before


# -- content can't break structure ---------------------------------------------


@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_content_cannot_add_sections_or_escape_the_fence(hostile):
    page = set_fenced(_project(), "Status", hostile)
    page = append_lines(page, "Decisions", hostile.splitlines())
    page = append_lines(page, "Notes", [hostile])
    reparsed = parse_page(dump_page(page))
    assert [s.title for s in reparsed.sections] == [s.title for s in _project().sections]
    assert reparsed.frontmatter.title == "Watcher"
    for title in ("Status", "Decisions"):
        body = reparsed.section(title).body
        assert body.count("<!-- /watcher -->") == 1
        assert body.count("<!-- watcher:") == 1


# -- missing sections ----------------------------------------------------------


def test_every_write_recreates_a_missing_section_at_its_schema_position():
    text = (FIXTURES / "project_watcher.md").read_text()
    start, end = text.index("## Decisions"), text.index("## Log")
    page = parse_page(text[:start] + text[end:])

    page = append_items(page, "Decisions", [Item("Ship it", "d-1", True, {"src": "1"})])
    titles = [s.title for s in page.sections]
    assert titles.index("Decisions") == titles.index("Open tasks") + 1
    assert "Ship it" in fenced_content(page, "Decisions")


# -- items ---------------------------------------------------------------------


def test_append_items_is_idempotent():
    items = [Item("Use gowa", "d-9", True, {"src": "5"}), Item("Friday demo", "d-8", True, {"src": "6"})]
    once = append_items(_project(), "Decisions", items)
    twice = append_items(once, "Decisions", items)
    assert dump_page(once) == dump_page(twice)
    assert len(parse_items(once.section("Decisions").body)) == 3


def test_upsert_items_replaces_by_block_id_and_keeps_prose():
    text = (FIXTURES / "project_watcher.md").read_text().replace(
        "^t-9f3c\n", "^t-9f3c\nA human's aside inside the fence.\n"
    )
    page = parse_page(text)
    ticked = Item("Book the seminar hall", "t-9f3c", True, {"src": "3EB1A2"})
    new = Item("Order pizza", "t-new", False, {"src": "3EB9"})
    page = upsert_items(page, "Open tasks", [ticked, new])
    body = fenced_content(page, "Open tasks")
    assert body.splitlines() == [
        "- [x] Book the seminar hall [src:: 3EB1A2] ^t-9f3c",
        "A human's aside inside the fence.",
        "- [ ] Order pizza [src:: 3EB9] ^t-new",
    ]


# -- frontmatter ---------------------------------------------------------------


def test_set_field_validates_against_the_schema():
    thread = _new_thread()
    assert set_field(thread, "state", "stale").frontmatter.state == "stale"
    with pytest.raises(InvalidPageEdit):
        set_field(thread, "state", "finished")


def test_set_field_keeps_the_frontmatter_model_current():
    thread = _new_thread()
    updated = set_field(thread, "message_ids", ["1", "2"])
    assert updated.frontmatter.message_ids == ["1", "2"]


@pytest.mark.parametrize(
    "title", ["Friday: the demo", "123", "2026-09-24", "multi\nline\n\ntitle", 'quote " and #']
)
def test_set_title_writes_any_text_safely(title):
    page = set_title(_project(), title)
    reparsed = parse_page(dump_page(page))
    flat = " ".join(title.split())
    assert reparsed.frontmatter.title == flat
    assert f"# {flat}\n" in reparsed.preamble
