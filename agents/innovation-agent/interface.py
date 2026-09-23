"""Innovation Agent interface (post-v1, ADR-0010 lifted for this piece).

Publishes generated Idea pitches as real wiki pages (Wiki Format Part 3:
idea) and reads back human verdicts for the redundancy check. First
piece rebuilt against `shared.wiki` instead of the standalone
`LapisAdapter`/`lapis_feedback_store.py` from the frozen prototype.
"""
from __future__ import annotations

import hashlib
from datetime import date

from schemas import Idea
from shared.wiki.interface import Item, VaultClient, format_item_line, parse_page
from shared.wiki.templates import render_new_idea_page, slugify


def _mint_evidence_id(claim: str, taken: set[str]) -> str:
    """Same deterministic approach as agents.wa_agent.assign._mint_block_id,
    kept independent per AGENTS.md (no cross-imports between agent
    packages) - `e-` prefix distinguishes evidence ids from task/item ids
    on other page types."""
    digest = hashlib.sha1(claim.strip().lower().encode()).hexdigest()
    for width in (4, 6, 8):
        candidate = f"e-{digest[:width]}"
        if candidate not in taken:
            return candidate
    return f"e-{digest[:12]}"


def _evidence_items(idea: Idea) -> list[Item]:
    taken: set[str] = set()
    items = []
    for ev in idea.evidence:
        block_id = _mint_evidence_id(ev.claim, taken)
        taken.add(block_id)
        fields = {"status": ev.status.value}
        if ev.source_ids:
            fields["from"] = ", ".join(f"[[{sid}]]" for sid in ev.source_ids)
        items.append(Item(text=ev.claim, block_id=block_id, fields=fields))
    return items


async def publish_idea(vault: VaultClient, idea: Idea) -> str:
    """Writes a new innovation/<date>-<slug>.md page. Returns its path.
    Only ever called for an idea that already survived the gate - a
    gated-out idea never gets a page at all."""
    today = date.today().strftime("%Y%m%d")
    slug = f"{today}-{slugify(idea.title)}"
    path = f"innovation/{slug}.md"
    content = render_new_idea_page(
        title=idea.title,
        lane=idea.origin.value,
        statement=idea.statement,
        why_now=idea.why_now,
        evidence=_evidence_items(idea),
        existing_leverage=idea.existing_leverage,
        skill_match_present=idea.skill_match_present,
        skill_match_missing=idea.skill_match_missing,
        risks=idea.risks,
        trigger=idea.trigger_source_ids,
        slug=slug,
    )
    await vault.write(path, content, base_revision="")
    return path


async def get_reviewed_ideas(vault: VaultClient, lane: str | None = None) -> list[dict]:
    """Every idea page with a human-set verdict. Replaces
    lapis_feedback_store.get_recent_feedback - reads real pages instead
    of a separate YAML-list file, so there's one canonical home for an
    idea's fate (Wiki Format principle 8), not two."""
    paths = await vault.list("innovation/")
    reviewed = []
    for path in paths:
        result = await vault.read(path)
        page = parse_page(result.content)
        fm = page.frontmatter
        if not fm.verdict:
            continue
        if lane and fm.lane != lane:
            continue
        reviewed.append({
            "path": path, "title": fm.title, "lane": fm.lane,
            "verdict": fm.verdict, "reason": fm.reason,
        })
    return reviewed