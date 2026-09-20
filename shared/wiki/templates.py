"""Page templates (Wiki Format, Part 3). Used by /setup to create a
channel + initiative page pair. Every managed/append/derived section
starts with an empty fence skeleton; `repair_missing_sections` (lint.py)
would produce the same shape for a page missing them.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "untitled"


def render_new_project_page(title: str, lead: str, slug: str | None = None) -> str:
    slug = slug or slugify(title)
    today = date.today().isoformat()
    return (
        "---\n"
        "type: project\n"
        f"slug: {slug}\n"
        f"title: {title}\n"
        "status: proposed\n"
        f'lead: "[[{lead}]]"\n'
        "members: []\n"
        f"started: {today}\n"
        "---\n"
        f"# {title}\n"
        "## Brief\n\n"
        "## Status\n<!-- watcher:managed -->\n<!-- /watcher -->\n"
        "## Open tasks\n<!-- watcher:managed -->\n<!-- /watcher -->\n"
        "## Decisions\n<!-- watcher:append -->\n<!-- /watcher -->\n"
        "## Log\n<!-- watcher:append -->\n<!-- /watcher -->\n"
        "## Resources\n<!-- watcher:append -->\n<!-- /watcher -->\n"
        "## Threads\n<!-- watcher:derived -->\n<!-- /watcher -->\n"
        "## Notes\n"
    )


def render_new_event_page(title: str, lead: str, slug: str | None = None) -> str:
    slug = slug or slugify(title)
    return (
        "---\n"
        "type: event\n"
        f"slug: {slug}\n"
        f"title: {title}\n"
        "status: proposed\n"
        f'lead: "[[{lead}]]"\n'
        "members: []\n"
        "---\n"
        f"# {title}\n"
        "## Brief\n\n"
        "## Status\n<!-- watcher:managed -->\n<!-- /watcher -->\n"
        "## Logistics\n<!-- watcher:managed -->\n<!-- /watcher -->\n"
        "## Decisions\n<!-- watcher:append -->\n<!-- /watcher -->\n"
        "## Log\n<!-- watcher:append -->\n<!-- /watcher -->\n"
        "## Resources\n<!-- watcher:append -->\n<!-- /watcher -->\n"
        "## Threads\n<!-- watcher:derived -->\n<!-- /watcher -->\n"
        "## Outcome\n\n"
        "## Notes\n"
    )


def render_new_member_page(title: str, role: str = "Executive", slug: str | None = None) -> str:
    slug = slug or slugify(title)
    return (
        "---\n"
        "type: member\n"
        f"slug: {slug}\n"
        f"title: {title}\n"
        f"role: {role}\n"
        "status: active\n"
        "whatsapp: linked\n"
        "---\n"
        f"# {title}\n"
        "## About\n\n"
        "## Projects\n<!-- watcher:derived -->\n<!-- /watcher -->\n"
        "## Past projects\n<!-- watcher:derived -->\n<!-- /watcher -->\n"
        "## Open tasks\n<!-- watcher:derived -->\n<!-- /watcher -->\n"
        "## Notes\n"
    )


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
) -> str:
    now = datetime.now(UTC).isoformat()
    initiative_line = f'initiative: "[[{initiative}]]"\n' if initiative else ""
    participants_list = ", ".join(f'"[[{p}]]"' for p in participants)
    message_ids_list = ", ".join(f'"{m}"' for m in message_ids)
    timeline_body = "\n".join(timeline_lines)
    return (
        "---\n"
        "type: thread\n"
        f"slug: {slug}\n"
        f'channel: "[[{channel_title}]]"\n'
        f"{initiative_line}"
        f"title: {title}\n"
        "state: active\n"
        f"opened_at: {now}\n"
        f"last_message_at: {now}\n"
        f"participants: [{participants_list}]\n"
        f"message_ids: [{message_ids_list}]\n"
        f"summary_cursor: {message_ids[-1] if message_ids else ''}\n"
        "---\n"
        f"# {title}\n"
        "## Summary\n<!-- watcher:managed -->\n"
        f"{summary}\n"
        "<!-- /watcher -->\n"
        "## Items\n<!-- watcher:managed -->\n"
        f"{items_text}\n"
        "<!-- /watcher -->\n"
        "## Timeline\n<!-- watcher:append -->\n"
        f"{timeline_body}\n"
        "<!-- /watcher -->\n"
        "## Notes\n"
    )


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
        f"title: {title}\n"
        f"kind: {kind}\n"
        f"{initiative_line}"
        "cursor:\n"
        f"last_batch: {now}\n"
        "---\n"
        f"# {title}\n"
        "## Active threads\n<!-- watcher:derived -->\n<!-- /watcher -->\n"
        "## Stale threads\n<!-- watcher:derived -->\n<!-- /watcher -->\n"
        "## Archived threads\n<!-- watcher:derived -->\n<!-- /watcher -->\n"
        "## Notes\n"
    )
