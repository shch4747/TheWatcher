"""Phase 1 lint + derived-regeneration acceptance criteria: lint runs
clean on well-formed pages; a hand-mangled page is flagged, not "fixed";
regeneration touches only derived sections and produces correct
Members.md / README.md content."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from shared.wiki.interface import (
    LintIssue,
    MemberIndexRow,
    NotDerivedError,
    ReadmeData,
    dump_page,
    lint_text,
    parse_page,
    render_lint_report,
    render_members_index,
    render_readme,
    repair_missing_sections,
    set_derived_section,
)

FIXTURES = Path(__file__).parent / "fixtures" / "wiki"


def test_lint_is_clean_on_well_formed_pages():
    for filename in ["member_aira.md", "project_watcher.md", "thread_demo_date.md"]:
        text = (FIXTURES / filename).read_text()
        issues = lint_text(filename, text)
        errors = [i for i in issues if i.severity == "error"]
        assert errors == [], f"{filename}: unexpected lint errors: {errors}"


def test_mangled_page_is_flagged_not_fixed():
    text = (FIXTURES / "project_mangled.md").read_text()
    issues = lint_text("project_mangled.md", text)

    # bad enum value on `status` -> a validation-error issue, page is
    # reported, never silently coerced to a valid value.
    assert any("not-a-real-status" in i.message or "status" in i.message for i in issues)

    # PII in the Brief section is caught.
    messages = " ".join(i.message for i in issues)
    assert "devansh@example.com" in messages
    assert "98765 43210" in messages

    # the source text itself is untouched - lint never repairs in place.
    assert (FIXTURES / "project_mangled.md").read_text() == text


def test_lint_report_renders_issues():
    issues = [LintIssue(path="a.md", severity="error", message="bad thing")]
    report = render_lint_report(issues)
    assert "a.md" in report
    assert "bad thing" in report
    assert render_lint_report([]) == "# Lint report\n\nNo issues.\n"


def test_dates_and_message_ids_are_not_flagged_as_pii():
    text = (FIXTURES / "project_watcher.md").read_text()
    issues = lint_text("project_watcher.md", text)
    errors = [i for i in issues if "phone" in i.message]
    assert errors == []


def test_repair_missing_sections_is_additive_only():
    # a valid but incomplete project page - missing frontmatter status is
    # the real problem in project_mangled, so build a minimal valid one
    # inline instead, to isolate the "missing sections" behaviour.
    text = (
        "---\ntype: project\nslug: bare\ntitle: Bare\nstatus: active\n"
        'lead: "[[Devansh]]"\n---\n# Bare\n## Brief\nHuman text stays put.\n'
    )
    page = parse_page(text)
    repaired = repair_missing_sections(page)

    titles = {s.title for s in repaired.sections}
    assert {"Status", "Open tasks", "Decisions", "Log", "Resources", "Threads"} <= titles
    # existing human section is untouched
    assert repaired.section("Brief").body == page.section("Brief").body


def test_set_derived_section_rejects_non_derived_owner():
    page = parse_page((FIXTURES / "project_watcher.md").read_text())
    try:
        set_derived_section(page, "Brief", "new content")
    except NotDerivedError:
        pass
    else:
        raise AssertionError("expected NotDerivedError for a human-owned section")


def test_set_derived_section_replaces_only_that_section():
    page = parse_page((FIXTURES / "project_watcher.md").read_text())
    original_brief = page.section("Brief").body

    new_body = "<!-- watcher:derived -->\nnew threads\n<!-- /watcher -->\n"
    updated = set_derived_section(page, "Threads", new_body)

    assert updated.section("Threads").body == new_body
    assert updated.section("Brief").body == original_brief
    # dump still parses back cleanly
    round_tripped = parse_page(dump_page(updated))
    assert round_tripped.section("Threads").body == new_body


def test_render_members_index():
    rows = [
        MemberIndexRow(title="Zara", role="Executive", status="active"),
        MemberIndexRow(title="Aira", role="Coordinator", status="active"),
    ]
    md = render_members_index(rows)
    assert md.index("Aira") < md.index("Zara")  # sorted
    assert "[[Aira]]" in md
    assert "Coordinator" in md


def test_render_readme_dashboard():
    data = ReadmeData(
        active_initiatives=["[[Watcher]]"],
        tasks_due=["Book the hall"],
        new_threads=[],
        upcoming_events=["Demo day"],
    )
    md = render_readme(data, as_of=date(2026, 9, 20))
    assert "2026-09-20" in md
    assert "[[Watcher]]" in md
    assert "Book the hall" in md
    assert "- (none)" in md  # empty new_threads section
    assert "Demo day" in md
