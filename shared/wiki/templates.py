"""Page templates (Wiki Format, Part 3): frontmatter for each new page
type. The sections come from `editor.new_page_sections`, i.e. from the
Page Schema itself, so a template can't declare a section, owner or
fence the editor wouldn't enforce.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime

from shared.wiki.editor import new_page_sections, yaml_str

SLUG_MAX_LEN = 80


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    slug = slug[:SLUG_MAX_LEN].rstrip("-")
    return slug or "untitled"


def render_new_project_page(title: str, lead: str, slug: str | None = None, brief: str = "") -> str:
    slug = slug or slugify(title)
    today = date.today().isoformat()
    return (
        "---\n"
        "type: project\n"
        f"slug: {slug}\n"
        f"title: {yaml_str(title)}\n"
        "status: proposed\n"
        f'lead: "[[{lead}]]"\n'
        "members: []\n"
        f"started: {today}\n"
        "---\n"
        f"# {title}\n"
    ) + new_page_sections("project", {"Brief": brief})


def render_new_event_page(title: str, lead: str, slug: str | None = None, brief: str = "") -> str:
    slug = slug or slugify(title)
    return (
        "---\n"
        "type: event\n"
        f"slug: {slug}\n"
        f"title: {yaml_str(title)}\n"
        "status: proposed\n"
        f'lead: "[[{lead}]]"\n'
        "members: []\n"
        "---\n"
        f"# {title}\n"
    ) + new_page_sections("event", {"Brief": brief})


def render_new_member_page(title: str, role: str = "Executive", slug: str | None = None) -> str:
    slug = slug or slugify(title)
    return (
        "---\n"
        "type: member\n"
        f"slug: {slug}\n"
        f"title: {yaml_str(title)}\n"
        f"role: {role}\n"
        "status: active\n"
        "whatsapp: linked\n"
        "---\n"
        f"# {title}\n"
    ) + new_page_sections("member")


def render_new_thread_page(
    slug: str,
    title: str,
    channel_title: str,
    initiative: str | None,
    summary: str,
    items_text: str,
    timeline_lines: list[str],
    participants: list[str],
    message_ids: list[str],
    opened_at: datetime | None = None,
    last_message_at: datetime | None = None,
) -> str:
    """`opened_at`/`last_message_at` are when the thread's first and last
    message were *sent*, not when we wrote the page - a backfilled
    conversation from last month must not look like it happened today
    (it drives the stale check and the channel index's "last <date>").
    They default to now for a caller that has no message times."""
    fallback = datetime.now(UTC)
    opened = (opened_at or fallback).isoformat()
    last = (last_message_at or opened_at or fallback).isoformat()
    initiative_line = f'initiative: "[[{initiative}]]"\n' if initiative else ""
    # Entries arrive ready to write - "[[Member]]" for a linked sender,
    # a bare display name otherwise - so quote, don't wrap.
    participants_list = ", ".join(yaml_str(p) for p in participants)
    message_ids_list = ", ".join(yaml_str(m) for m in message_ids)
    timeline_body = "\n".join(timeline_lines)
    return (
        "---\n"
        "type: thread\n"
        f"slug: {slug}\n"
        f'channel: "[[{channel_title}]]"\n'
        f"{initiative_line}"
        f"title: {yaml_str(title)}\n"
        "state: active\n"
        f"opened_at: {opened}\n"
        f"last_message_at: {last}\n"
        f"participants: [{participants_list}]\n"
        f"message_ids: [{message_ids_list}]\n"
        f"summary_cursor: {yaml_str(message_ids[-1]) if message_ids else ''}\n"
        "---\n"
        f"# {title}\n"
    ) + new_page_sections("thread", {"Summary": summary, "Items": items_text, "Timeline": timeline_body})


def render_new_channel_page(
    title: str, kind: str, slug: str | None = None, initiative: str | None = None
) -> str:
    slug = slug or slugify(title)
    now = datetime.now(UTC).isoformat()
    initiative_line = f'initiative: "[[{initiative}]]"\n' if initiative else ""
    return (
        "---\n"
        "type: channel\n"
        f"slug: {slug}\n"
        f"title: {yaml_str(title)}\n"
        f"kind: {kind}\n"
        f"{initiative_line}"
        "cursor:\n"
        f"last_batch: {now}\n"
        "---\n"
        f"# {title}\n"
    ) + new_page_sections("channel")
