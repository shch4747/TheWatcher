"""Project Agent v1 interface (Spec: "Project Agent v1" - wiki
maintenance only; health score/nudges/GitHub are After v1). Works
through its Inbox (`inbox/project_agent.md`, ADR-0014), carries thread
Items onto the initiative page with stable ids, reads back human edits,
rewrites `## Status`. Confirmed Proposals are executed by the WhatsApp
Agent's Proposals module, not here. Projects and Events are handled by the same code path; the only
difference is which section holds the task checklist (`Open tasks` vs
`Logistics` - see `_TASK_SECTION_BY_TYPE` below).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from shared.gateway.interface import get_channel
from shared.inbox.interface import THREAD_UPDATE, Inbox, InboxItem
from shared.models.interface import generate
from shared.models.text import TextModelClient
from shared.wiki.interface import (
    Item,
    ThreadStore,
    VaultClient,
    append_items,
    dump_page,
    fenced_content,
    initiative_page_path,
    parse_items,
    parse_page,
    set_fenced,
    upsert_items,
)

logger = logging.getLogger(__name__)

AGENT = "project_agent"

# Wiki Format Part 3: project has "Open tasks", event has "Logistics" -
# same role (managed checklist), different canonical name.
_TASK_SECTION_BY_TYPE = {"project": "Open tasks", "event": "Logistics"}


@dataclass
class ApplyResult:
    tasks_upserted: int = 0
    decisions_appended: int = 0
    resources_appended: int = 0
    log_lines_appended: int = 0
    skipped_no_target_section: list[str] = field(default_factory=list)


def _merge_item(existing: Item, incoming: Item) -> Item:
    """Read-back for human edits (Spec: "reads back human edits (ticked
    boxes, changed dates)"): a human-ticked box never gets unticked by
    the agent, and a human-set owner/due survives unless the item never
    had one before. True edit-provenance tracking is out of v1 scope -
    this is the simplest policy that can't silently discard a human's
    tick or date change.
    """
    checked = existing.checked if existing.checked else incoming.checked
    fields = dict(incoming.fields)
    for key in ("due", "owner"):
        if key in existing.fields:
            fields[key] = existing.fields[key]
    return Item(text=incoming.text, block_id=incoming.block_id, checked=checked, fields=fields)


def _grown(before, after, title: str) -> int:
    return len(parse_items(after.section(title).body)) - len(parse_items(before.section(title).body))


async def apply_thread_items_to_initiative(
    vault: VaultClient, thread_path: str, initiative_path: str
) -> ApplyResult:
    """Carry a Thread's Items onto its Initiative page, idempotently:
    tasks are upserted into Open tasks/Logistics by block id (human ticks
    and dates survive), decisions, resources and questions are appended
    to Decisions, Resources and Log only if that block id (or text) isn't
    there yet - so the same Thread applied twice changes nothing the
    second time. A question has no section of its own in the v1 page
    shape, so it goes to the Log (Spec user story 13)."""
    result = ApplyResult()

    thread_page = parse_page((await vault.read(thread_path)).content)
    incoming = parse_items(fenced_content(thread_page, "Items"))
    if not incoming:
        return result

    initiative = await vault.read(initiative_path)
    page = parse_page(initiative.content)
    task_section = _TASK_SECTION_BY_TYPE.get(page.page_type)

    by_kind: dict[str, list[Item]] = {}
    for item in incoming:
        by_kind.setdefault(item.fields.get("kind", "task"), []).append(item)

    def _src(item: Item) -> dict[str, str]:
        return {"src": ", ".join(item.src_ids())}

    if "task" in by_kind:
        if task_section and page.section(task_section) is not None:
            page = upsert_items(page, task_section, by_kind["task"], merge=_merge_item)
            result.tasks_upserted = len(by_kind["task"])
        else:
            result.skipped_no_target_section.append("task")

    appends = {
        "Decisions": [
            Item(i.text, i.block_id, None, {"by": "system", **_src(i)}) for i in by_kind.get("decision", [])
        ],
        "Resources": [Item(i.text, i.block_id, None, _src(i)) for i in by_kind.get("resource", [])],
        "Log": [
            Item(f"Open question: {i.text}", i.block_id, None, _src(i)) for i in by_kind.get("question", [])
        ],
    }
    counts = {
        "Decisions": "decisions_appended",
        "Resources": "resources_appended",
        "Log": "log_lines_appended",
    }
    for title, items in appends.items():
        if items:
            before = page
            page = append_items(page, title, items)
            setattr(result, counts[title], _grown(before, page, title))

    new_content = dump_page(page)
    if new_content != initiative.content:
        await vault.write(initiative_path, new_content, base_revision=initiative.revision)
    return result


async def rewrite_status(vault: VaultClient, initiative_path: str, worker_client: TextModelClient) -> None:
    """`## Status` rewritten each run (Wiki Format: managed, <=5
    sentences) from the page's own Open tasks/Logistics + Decisions."""
    result = await vault.read(initiative_path)
    page = parse_page(result.content)
    task_section_title = _TASK_SECTION_BY_TYPE.get(page.page_type)
    tasks_body = fenced_content(page, task_section_title) if task_section_title else ""
    decisions_body = fenced_content(page, "Decisions")

    prompt = (
        f"Open tasks:\n{tasks_body}\n\nRecent decisions:\n{decisions_body}\n\n"
        "Write a status update in at most 5 sentences."
    )
    text_result = await generate(worker_client, "worker", prompt)
    page = set_fenced(page, "Status", text_result.text.strip())
    await vault.write(initiative_path, dump_page(page), base_revision=result.revision)


async def _apply_update(
    vault: VaultClient, item: InboxItem, worker_client: TextModelClient
) -> ApplyResult | None:
    """One Update Notice. None if there's nothing to apply it to (the
    Channel is gone or has no Initiative, or the Thread no longer exists)
    - that's done, not failed."""
    channel = await get_channel(item.channel) if item.channel else None
    if channel is None or not channel.initiative or not item.thread_slug:
        return None
    thread = await ThreadStore(vault, channel).get(item.thread_slug)
    if thread is None:
        return None
    initiative_path = initiative_page_path(channel.kind, channel.initiative)
    result = await apply_thread_items_to_initiative(vault, thread.path, initiative_path)
    await rewrite_status(vault, initiative_path, worker_client)
    return result


async def run_project_agent_once(vault: VaultClient, worker_client: TextModelClient) -> list[ApplyResult]:
    """Work through the Inbox (Spec: "pull my Inbox and receive Update
    Notices"). Each item is acknowledged only once it has been applied;
    one that fails is logged and stays pending for the next run."""
    inbox = Inbox(vault, AGENT)
    results: list[ApplyResult] = []
    done: list[str] = []
    for item in await inbox.pending():
        if item.kind != THREAD_UPDATE:
            logger.info("project agent: leaving %s item %s (%s) for a human", item.kind, item.id, item.text)
            continue
        try:
            result = await _apply_update(vault, item, worker_client)
        except Exception:  # noqa: BLE001 - one bad item must not block the rest
            logger.exception("project agent: applying inbox item %s failed; will retry", item.id)
            continue
        if result is not None:
            results.append(result)
        done.append(item.id)
    await inbox.ack(done)
    return results
