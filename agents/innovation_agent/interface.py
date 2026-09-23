"""Innovation Agent interface (post-v1, ADR-0010 lifted for this piece).

Publishes generated Idea pitches as real wiki pages (Wiki Format Part 3:
idea) and reads back human verdicts for the redundancy check. First
piece rebuilt against `shared.wiki` instead of the standalone
`LapisAdapter`/`lapis_feedback_store.py` from the frozen prototype.
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import date

from schemas import Idea
from shared.wiki.interface import (
    Item,
    VaultClient,
    parse_page,
    read_if_exists,
    render_new_idea_page,
    slugify,
)


def _mint_evidence_id(claim: str, taken: set[str]) -> str:
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
    await vault.create(path, content)
    return path


async def get_closed_project_causes(vault: VaultClient) -> dict[str, str]:
    causes: dict[str, str] = {}
    for path in await vault.list("projects/"):
        result = await vault.read(path)
        fm = parse_page(result.content).frontmatter
        if fm.status == "dead" and fm.cause:
            causes[path] = fm.cause
    return causes


async def get_capability_usage(vault: VaultClient) -> dict[str, list[str]]:
    usage: dict[str, list[str]] = {}
    for path in await vault.list("projects/"):
        result = await vault.read(path)
        fm = parse_page(result.content).frontmatter
        for tech in fm.technologies:
            usage.setdefault(tech, []).append(path)
    return usage


async def get_reviewed_ideas(vault: VaultClient, lane: str | None = None) -> list[dict]:
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


async def get_record(vault: VaultClient, path: str) -> dict | None:
    result = await read_if_exists(vault, path)
    if result is None:
        return None
    return {"id": path, "content": result.content}


def publish_idea_sync(vault: VaultClient, idea: Idea) -> str:
    return asyncio.run(publish_idea(vault, idea))


def get_reviewed_ideas_sync(vault: VaultClient, lane: str | None = None) -> list[dict]:
    return asyncio.run(get_reviewed_ideas(vault, lane))


def get_closed_project_causes_sync(vault: VaultClient) -> dict[str, str]:
    return asyncio.run(get_closed_project_causes(vault))


def get_capability_usage_sync(vault: VaultClient) -> dict[str, list[str]]:
    return asyncio.run(get_capability_usage(vault))


def get_record_sync(vault: VaultClient, path: str) -> dict | None:
    return asyncio.run(get_record(vault, path))