"""Lint job (Wiki Format principle 12: reports, does not repair - except
purely additive defaults). Validates a page's frontmatter (via the
schema, which already rejects bad values at parse time - lint's job is
to catch and report that instead of letting the pipeline crash), scans
for PII (ADR-0009: phone/email-shaped strings are never allowed in the
wiki), and flags missing managed/append/derived section skeletons as
additively repairable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from shared.wiki.parser import Page, Section, WikiParseError, parse_page_lenient
from shared.wiki.schema import SECTION_OWNERS

Severity = Literal["error", "warning"]

_PHONE_CANDIDATE_RE = re.compile(r"(?<!\w)\+?\d[\d\-\s()]{7,}\d(?!\w)")
_ISO_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(\s+\d{1,2}:\d{2})?$|^\d{4}-\d{2}-\d{2}\s+\d{1,2}$")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _looks_like_phone(candidate: str) -> bool:
    """Filters ISO dates/timestamps (2026-09-24, "2026-09-18 10") and other
    digit runs that aren't phone numbers out of the PHONE_CANDIDATE matches."""
    if _ISO_DATE_PREFIX_RE.match(candidate):
        return False
    digit_count = sum(c.isdigit() for c in candidate)
    return 7 <= digit_count <= 15


@dataclass
class LintIssue:
    path: str
    severity: Severity
    message: str


def _pii_issues(path: str, page: Page) -> list[LintIssue]:
    issues = []
    for section in page.sections:
        for m in _PHONE_CANDIDATE_RE.finditer(section.body):
            if not _looks_like_phone(m.group(0)):
                continue
            issues.append(
                LintIssue(
                    path=path,
                    severity="error",
                    message=(
                        f"possible phone-shaped string in section "
                        f"'{section.title}': {m.group(0)!r} (ADR-0009: no PII in the wiki)"
                    ),
                )
            )
        for m in _EMAIL_RE.finditer(section.body):
            issues.append(
                LintIssue(
                    path=path,
                    severity="error",
                    message=(
                        f"possible email-shaped string in section "
                        f"'{section.title}': {m.group(0)!r} (ADR-0009: no PII in the wiki)"
                    ),
                )
            )
    return issues


def _missing_section_issues(path: str, page: Page) -> list[LintIssue]:
    owners = SECTION_OWNERS.get(page.page_type, {})
    present = {s.title.lower() for s in page.sections}
    issues = []
    for title, owner in owners.items():
        if owner in ("managed", "append", "derived") and title.lower() not in present:
            issues.append(
                LintIssue(
                    path=path,
                    severity="warning",
                    message=f"missing {owner} section '{title}' (additive repair available)",
                )
            )
    return issues


def lint_text(path: str, text: str) -> list[LintIssue]:
    """Lint raw page text without letting a bad page take down the run -
    a structural failure (no frontmatter at all) still aborts, since
    there's nothing to scan; a *validation* failure (bad frontmatter
    value) is reported alongside PII/missing-section checks on whatever
    section structure was still recovered."""
    try:
        page, validation_error = parse_page_lenient(text)
    except WikiParseError as exc:
        return [LintIssue(path=path, severity="error", message=str(exc))]

    issues = []
    if validation_error is not None:
        issues.append(LintIssue(path=path, severity="error", message=str(validation_error)))
    issues += _pii_issues(path, page) + _missing_section_issues(path, page)
    return issues


def repair_missing_sections(page: Page) -> Page:
    """Appends an empty skeleton for each missing managed/append/derived
    section. Never touches a section that already exists, human/shared
    sections are left for a human to add (Wiki Format principle 12)."""
    owners = SECTION_OWNERS.get(page.page_type, {})
    present = {s.title.lower() for s in page.sections}
    new_sections = list(page.sections)
    for title, owner in owners.items():
        if owner in ("managed", "append", "derived") and title.lower() not in present:
            new_sections.append(
                Section(
                    title=title,
                    header_raw=f"## {title}\n",
                    body=f"<!-- watcher:{owner} -->\n<!-- /watcher -->\n",
                )
            )
    return Page(
        page_type=page.page_type,
        frontmatter=page.frontmatter,
        frontmatter_raw=page.frontmatter_raw,
        preamble=page.preamble,
        sections=new_sections,
    )


def render_lint_report(issues: list[LintIssue]) -> str:
    """`meta/lint.md` body."""
    if not issues:
        return "# Lint report\n\nNo issues.\n"
    lines = ["# Lint report", ""]
    for issue in sorted(issues, key=lambda i: (i.severity != "error", i.path)):
        lines.append(f"- **{issue.severity}** `{issue.path}`: {issue.message}")
    return "\n".join(lines) + "\n"
