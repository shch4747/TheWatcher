"""Project Agent v1 interface (Spec: "Project Agent v1" - wiki
maintenance only; health score/nudges/GitHub are After v1). Consumes its
Inbox, upserts thread Items into the initiative page's managed sections
with stable ids, reads back human edits, rewrites `## Status`. Proposal
execution (Spec: "executes confirmed Proposals through the wiki layer")
is already `shared.gateway.interface.confirm_proposal` - there's no
separate execution path here, Project Agent doesn't own the Proposal
table. Projects and Events are handled by the same code path; the only
difference is which section holds the task checklist (`Open tasks` vs
`Logistics` - see `_TASK_SECTION_BY_TYPE` below).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from shared.db import Notice, get_session
from shared.gateway.interface import get_channel
from shared.models.interface import generate
from shared.models.text import TextModelClient
from shared.wiki.interface import (
    Item,
    VaultClient,
    append_lines,
    dump_page,
    format_item_line,
    parse_items,
    parse_page,
    set_fenced,
    slugify,
)
from sqlalchemy import select

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


async def consume_inbox(agent: str = "project_agent") -> list[Notice]:
    """Unconsumed Update Notices for this agent (Spec: "pull my Inbox").
    Marks them consumed - callers that fail partway should re-derive
    what to do from the wiki state, not assume an unconsumed notice will
    come back (idempotent processing, same principle as batch cutting)."""
    now = datetime.now(UTC)
    async with get_session() as session:
        rows = list(
            await session.scalars(
                select(Notice).where(Notice.agent == agent, Notice.consumed_at.is_(None))
            )
        )
        for row in rows:
            row.consumed_at = now
        await session.commit()
    return rows


def _merge_item(existing: Item | None, incoming: Item) -> Item:
    """Read-back for human edits (Spec: "reads back human edits (ticked
    boxes, changed dates)"): a human-ticked box never gets unticked by
    the agent, and a human-set owner/due survives unless the item never
    had one before. True edit-provenance tracking is out of v1 scope -
    this is the simplest policy that can't silently discard a human's
    tick or date change.
    """
    if existing is None:
        return incoming
    checked = existing.checked if existing.checked else incoming.checked
    fields = dict(incoming.fields)
    for key in ("due", "owner"):
        if key in existing.fields:
            fields[key] = existing.fields[key]
    return Item(text=incoming.text, block_id=incoming.block_id, checked=checked, fields=fields)


def _upsert_items(existing_body: str, incoming: list[Item]) -> str:
    existing_items = {item.block_id: item for item in parse_items(existing_body)}
    lines = existing_body.splitlines()
    seen_incoming_ids = {item.block_id for item in incoming}

    # Keep every existing line untouched unless it's being upserted -
    # rebuild only the lines that correspond to items, preserving
    # anything else (blank lines, stray prose) verbatim.
    output_lines = []
    replaced_ids: set[str] = set()
    for line in lines:
        parsed = None
        for item in existing_items.values():
            if format_item_line(item) == line.rstrip():
                parsed = item
                break
        if parsed is not None and parsed.block_id in seen_incoming_ids:
            merged = _merge_item(parsed, next(i for i in incoming if i.block_id == parsed.block_id))
            output_lines.append(format_item_line(merged))
            replaced_ids.add(parsed.block_id)
        else:
            output_lines.append(line)

    for item in incoming:
        if item.block_id not in replaced_ids and item.block_id not in existing_items:
            output_lines.append(format_item_line(item))

    return "\n".join(output_lines).strip("\n") + "\n"


async def apply_thread_items_to_initiative(
    vault: VaultClient, thread_path: str, initiative_path: str
) -> ApplyResult:
    """Upserts a thread's Items into the initiative page's managed
    sections by kind: task -> Open tasks/Logistics, decision ->
    Decisions, resource -> Resources; a question with no dedicated
    section in the v1 page shape goes to the Log so it stays visible
    (Spec user story 13) without inventing a section the schema doesn't
    have."""
    result = ApplyResult()

    thread_result = await vault.read(thread_path)
    thread_page = parse_page(thread_result.content)
    items_section = thread_page.section("Items")
    if items_section is None:
        return result
    incoming_items = parse_items(items_section.body)
    if not incoming_items:
        return result

    initiative_result = await vault.read(initiative_path)
    page = parse_page(initiative_result.content)
    task_section_title = _TASK_SECTION_BY_TYPE.get(page.page_type)

    by_kind: dict[str, list[Item]] = {}
    for item in incoming_items:
        by_kind.setdefault(item.fields.get("kind", "task"), []).append(item)

    if "task" in by_kind and task_section_title:
        section = page.section(task_section_title)
        if section is not None:
            new_body = _upsert_items(section.body, by_kind["task"])
            page = set_fenced(page, task_section_title, new_body.strip())
            result.tasks_upserted = len(by_kind["task"])
    elif "task" in by_kind:
        result.skipped_no_target_section.append("task")

    if "decision" in by_kind:
        lines = [
            f"- {i.text} [by:: system] [src:: {','.join(i.src_ids())}] ^{i.block_id}"
            for i in by_kind["decision"]
        ]
        page = append_lines(page, "Decisions", lines)
        result.decisions_appended = len(lines)

    if "resource" in by_kind:
        lines = [f"- {i.text} [src:: {','.join(i.src_ids())}]" for i in by_kind["resource"]]
        page = append_lines(page, "Resources", lines)
        result.resources_appended = len(lines)

    if "question" in by_kind:
        lines = [f"- Open question: {i.text} [src:: {','.join(i.src_ids())}]" for i in by_kind["question"]]
        page = append_lines(page, "Log", lines)
        result.log_lines_appended = len(lines)

    await vault.write(initiative_path, dump_page(page), base_revision=initiative_result.revision)
    return result


async def rewrite_status(vault: VaultClient, initiative_path: str, worker_client: TextModelClient) -> None:
    """`## Status` rewritten each run (Wiki Format: managed, <=5
    sentences) from the page's own Open tasks/Logistics + Decisions."""
    result = await vault.read(initiative_path)
    page = parse_page(result.content)
    task_section_title = _TASK_SECTION_BY_TYPE.get(page.page_type)
    tasks_section = page.section(task_section_title) if task_section_title else None
    tasks_body = tasks_section.body if tasks_section else ""
    decisions_section = page.section("Decisions")
    decisions_body = decisions_section.body if decisions_section else ""

    prompt = (
        f"Open tasks:\n{tasks_body}\n\nRecent decisions:\n{decisions_body}\n\n"
        "Write a status update in at most 5 sentences."
    )
    text_result = await generate(worker_client, "worker", prompt)
    page = set_fenced(page, "Status", text_result.text.strip())
    await vault.write(initiative_path, dump_page(page), base_revision=result.revision)


async def _paths_for_notice(notice: Notice) -> tuple[str, str] | None:
    """(thread_path, initiative_path) for a Notice, derived the same way
    `agents.wa_agent` names paths (slugify(channel title or jid) for the
    channel dir, slugify(initiative) for the initiative page) - kept
    independent of that package rather than imported from it (AGENTS.md:
    no cross-imports between the two agent packages)."""
    channel = await get_channel(notice.channel)
    if channel is None or not channel.initiative:
        return None
    channel_dir = slugify(channel.title or channel.jid)
    thread_path = f"channels/{channel_dir}/{notice.thread_slug}.md"
    initiative_dir = "projects" if channel.kind == "project" else "events"
    initiative_path = f"{initiative_dir}/{slugify(channel.initiative)}.md"
    return thread_path, initiative_path


async def run_project_agent_once(vault: VaultClient, worker_client: TextModelClient) -> list[ApplyResult]:
    """Consume the Inbox and process every pending Update Notice (Spec:
    "pull my Inbox and receive Update Notices")."""
    notices = await consume_inbox()
    results = []
    for notice in notices:
        paths = await _paths_for_notice(notice)
        if paths is None:
            continue
        thread_path, initiative_path = paths
        result = await apply_thread_items_to_initiative(vault, thread_path, initiative_path)
        await rewrite_status(vault, initiative_path, worker_client)
        results.append(result)
    return results
