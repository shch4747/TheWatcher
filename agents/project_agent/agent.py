"""Project Agent: consume inbox notices, carry thread items onto the
initiative page, rewrite Status.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from shared.cms.interface import default_cms_client
from shared.gateway.interface import get_channel
from shared.inbox.interface import THREAD_UPDATE, Inbox, InboxItem
from shared.models.interface import TextModelClient, generate
from shared.observability.interface import PROJECT_AGENT, scope
from shared.wiki.interface import (
    Item,
    ThreadStore,
    VaultClient,
    append_items,
    apply_member_wikilinks,
    dump_page,
    fenced_content,
    initiative_page_path,
    parse_items,
    parse_page,
    set_fenced,
    upsert_items,
)

_WIKILINK_TITLE_RE = re.compile(r"\[\[([^\]|#]+)")

logger = logging.getLogger(__name__)

AGENT = "project_agent"
_TASK_SECTION_BY_TYPE = {"project": "Open tasks", "event": "Logistics"}


@dataclass
class ApplyResult:
    tasks_upserted: int = 0
    decisions_appended: int = 0
    resources_appended: int = 0
    log_lines_appended: int = 0
    skipped_no_target_section: list[str] = field(default_factory=list)


def merge_item(existing: Item, incoming: Item) -> Item:
    """A human-ticked box never gets unticked; a human-set owner/due survives."""
    checked = existing.checked if existing.checked else incoming.checked
    fields = dict(incoming.fields)
    for key in ("due", "owner"):
        if key in existing.fields:
            fields[key] = existing.fields[key]
    return Item(text=incoming.text, block_id=incoming.block_id, checked=checked, fields=fields)


def _grown(before, after, title: str) -> int:
    return len(parse_items(after.section(title).body)) - len(parse_items(before.section(title).body))


def _src(item: Item) -> dict[str, str]:
    return {"src": ", ".join(item.src_ids())}


def apply_task_items(page, items: list[Item], result: ApplyResult):
    task_section = _TASK_SECTION_BY_TYPE.get(page.page_type)
    if task_section and page.section(task_section) is not None:
        page = upsert_items(page, task_section, items, merge=merge_item)
        result.tasks_upserted = len(items)
        return page
    result.skipped_no_target_section.append("task")
    return page


def apply_decision_items(page, items: list[Item], result: ApplyResult):
    mapped = [Item(i.text, i.block_id, None, {"by": "system", **_src(i)}) for i in items]
    before = page
    page = append_items(page, "Decisions", mapped)
    result.decisions_appended = _grown(before, page, "Decisions")
    return page


def apply_resource_items(page, items: list[Item], result: ApplyResult):
    mapped = [Item(i.text, i.block_id, None, _src(i)) for i in items]
    before = page
    page = append_items(page, "Resources", mapped)
    result.resources_appended = _grown(before, page, "Resources")
    return page


def apply_question_items(page, items: list[Item], result: ApplyResult):
    mapped = [Item(f"Open question: {i.text}", i.block_id, None, _src(i)) for i in items]
    before = page
    page = append_items(page, "Log", mapped)
    result.log_lines_appended = _grown(before, page, "Log")
    return page


_KIND_HANDLERS = {
    "task": apply_task_items,
    "decision": apply_decision_items,
    "resource": apply_resource_items,
    "question": apply_question_items,
}


async def apply_thread_items_to_initiative(
    vault: VaultClient, thread_path: str, initiative_path: str
) -> ApplyResult:
    result = ApplyResult()
    thread_page = parse_page((await vault.read(thread_path)).content)
    incoming = parse_items(fenced_content(thread_page, "Items"))
    if not incoming:
        return result

    initiative = await vault.read(initiative_path)
    page = parse_page(initiative.content)

    by_kind: dict[str, list[Item]] = {}
    for item in incoming:
        by_kind.setdefault(item.fields.get("kind", "task"), []).append(item)

    for kind, items in by_kind.items():
        handler = _KIND_HANDLERS.get(kind)
        if handler is None:
            continue
        page = handler(page, items, result)

    new_content = dump_page(page)
    if new_content != initiative.content:
        await vault.write(initiative_path, new_content, base_revision=initiative.revision)
    return result


async def _member_aliases(page_text: str) -> dict[str, str]:
    """Titles already on the page, plus the ARIES roster, so a status
    line that says "Aira" is stored as [[Aira]]."""
    aliases = {
        title.strip(): title.strip() for title in _WIKILINK_TITLE_RE.findall(page_text) if title.strip()
    }
    try:
        for member in await default_cms_client().list_members():
            if member.title.strip():
                aliases[member.title.strip()] = member.title.strip()
    except Exception:  # noqa: BLE001 - the page's own wikilinks still apply
        logger.exception("project agent: member roster unavailable; linking names already on the page")
    return aliases


async def rewrite_status(vault: VaultClient, initiative_path: str, worker_client: TextModelClient) -> None:
    result = await vault.read(initiative_path)
    page = parse_page(result.content)
    task_section_title = _TASK_SECTION_BY_TYPE.get(page.page_type)
    aliases = await _member_aliases(result.content)
    tasks_body = fenced_content(page, task_section_title) if task_section_title else ""
    decisions_body = fenced_content(page, "Decisions")
    tasks_body = apply_member_wikilinks(tasks_body, aliases)
    decisions_body = apply_member_wikilinks(decisions_body, aliases)

    prompt = (
        f"Open tasks:\n{tasks_body}\n\nRecent decisions:\n{decisions_body}\n\n"
        "Write a status update in at most 5 sentences. "
        "When you name a member, use their [[wikilink]] exactly as it appears above."
    )
    with scope(phase=PROJECT_AGENT):
        text_result = await generate(worker_client, "worker", prompt)
    status = apply_member_wikilinks(text_result.text.strip(), aliases)
    page = set_fenced(page, "Status", status)
    await vault.write(initiative_path, dump_page(page), base_revision=result.revision)


class ProjectAgent:
    def __init__(self, vault: VaultClient, worker_client: TextModelClient) -> None:
        self.vault = vault
        self.worker_client = worker_client
        self.inbox = Inbox(vault, AGENT)

    async def run_once(self) -> list[ApplyResult]:
        results: list[ApplyResult] = []
        done: list[str] = []
        for item in await self.inbox.pending():
            if item.kind != THREAD_UPDATE:
                logger.info(
                    "project agent: leaving %s item %s (%s) for a human", item.kind, item.id, item.text
                )
                continue
            try:
                result = await self._apply_update(item)
            except Exception:  # noqa: BLE001 - one bad item must not block the rest
                logger.exception("project agent: applying inbox item %s failed; will retry", item.id)
                continue
            if result is not None:
                results.append(result)
            done.append(item.id)
        await self.inbox.ack(done)
        return results

    async def _apply_update(self, item: InboxItem) -> ApplyResult | None:
        channel = await get_channel(item.channel) if item.channel else None
        if channel is None or not channel.initiative or not item.thread_slug:
            return None
        thread = await ThreadStore(self.vault, channel).get(item.thread_slug)
        if thread is None:
            return None
        initiative_path = initiative_page_path(channel.kind, channel.initiative)
        result = await apply_thread_items_to_initiative(self.vault, thread.path, initiative_path)
        await rewrite_status(self.vault, initiative_path, self.worker_client)
        return result
