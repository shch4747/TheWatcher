#!/usr/bin/env python3
"""Wipe all thread data and ingestion state so batch-cutting starts
fresh - for recovering from a bad batch (e.g. slop written before the
sanitizers in agents/wa_agent/interface.py existed, or threads cut by
the pre-ADR-0012 pipeline) without touching channel registration or
initiative pages.

For every watched channel (shared.gateway.interface.list_channels):
  - deletes every thread page under `channels/<channel_dir>/` in the
    vault (Lapis if configured, else the local VAULT_ROOT directory),
    and, with --include-archive, the ended ones under
    `channels/archive/<channel_dir>/` too (those were ended by a human,
    so they are kept by default).
  - replaces the channel's own `channels/<slug>.md` index page with a
    fresh empty one - its Active/Stale/Archived sections are derived
    from thread pages that no longer exist, and nothing regenerates
    them until the next batch writes a thread.
  - resets the channel's `cursor` to NULL.
  - marks every buffered message for that channel `processed = False`,
    so the next `/ingest` (or the scheduled ingest_tick) re-cuts the
    full history into fresh threads.
  - acknowledges any pending project_agent Inbox Items for that
    channel, since they'd otherwise point at threads that no longer exist.

A channel page's `## Notes` (shared, human-owned) is carried over to the
rebuilt page; everything else on it is derived and regenerated.

Run this with the app stopped. `run_job`'s lock is per-process, so a
reset racing the running container's `ingest_tick` can have both cut the
same messages.

Usage:
    uv run python scripts/reset_threads.py                    # asks to confirm
    uv run python scripts/reset_threads.py --yes              # no prompt
    uv run python scripts/reset_threads.py --yes --include-archive
"""
from __future__ import annotations

import asyncio
import sys

from shared.db import Channel, MessageBuffer, get_session, init_db
from shared.gateway.interface import list_channels
from shared.inbox.interface import Inbox
from shared.wiki.interface import (
    archive_dir,
    channel_dir,
    channel_page_path,
    default_vault_client,
    parse_page_lenient,
    render_new_channel_page,
    thread_dir,
)
from sqlalchemy import select, update


async def _reset_channel_page(vault, channel: Channel) -> str:
    """Delete and rewrite the channel's index page, keeping its Notes."""
    slug = channel_dir(channel)
    path = channel_page_path(channel)
    notes = ""
    revision = ""
    try:
        existing = await vault.read(path)
        revision = existing.revision
        page, _ = parse_page_lenient(existing.content)
        section = page.section("Notes")
        notes = section.body.strip() if section else ""
    except Exception:  # noqa: BLE001 - no page yet, or one we can't parse
        pass

    fresh = render_new_channel_page(
        channel.title or channel.jid, channel.kind, slug=slug, initiative=channel.initiative
    )
    if notes:
        fresh = fresh.replace("## Notes\n", f"## Notes\n{notes}\n", 1)
    if revision:
        await vault.write(path, fresh, base_revision=revision)
    else:
        await vault.create(path, fresh)
    return path


async def main(skip_confirm: bool, include_archive: bool) -> None:
    await init_db()
    vault = default_vault_client()
    channels = await list_channels()

    if not channels:
        print("No watched channels - nothing to reset.")
        return

    print(f"About to reset {len(channels)} channel(s):")
    for c in channels:
        print(f"  - {c.jid}  kind={c.kind}  title={c.title or '-'}")
    print(
        "This deletes their thread pages"
        + (" (including archived ones)" if include_archive else "")
        + " and rebuilds their index pages."
    )

    if not skip_confirm:
        reply = input("Delete all their thread pages and reset cursors? [y/N] ").strip().lower()
        if reply != "y":
            print("Aborted.")
            return

    total_threads_deleted = 0
    for channel in channels:
        paths = await vault.list(thread_dir(channel))
        if include_archive:
            paths += await vault.list(archive_dir(channel))
        for path in paths:
            await vault.delete(path)
        total_threads_deleted += len(paths)

        page_path = await _reset_channel_page(vault, channel)

        async with get_session() as session:
            row = await session.get(Channel, channel.id)
            assert row is not None
            row.cursor = None
            await session.execute(
                update(MessageBuffer).where(MessageBuffer.channel == channel.jid).values(processed=False)
            )
            await session.commit()
        inbox = Inbox(vault, "project_agent")
        await inbox.ack([i.id for i in await inbox.pending() if i.channel == channel.jid])

        async with get_session() as session:
            unprocessed_count = len(
                list(
                    await session.scalars(
                        select(MessageBuffer).where(
                            MessageBuffer.channel == channel.jid, MessageBuffer.processed.is_(False)
                        )
                    )
                )
            )
        print(
            f"{channel.jid}: deleted {len(paths)} thread page(s), rebuilt {page_path}, reset cursor, "
            f"{unprocessed_count} message(s) now unprocessed"
        )

    print(f"\nDone. {total_threads_deleted} thread page(s) deleted across {len(channels)} channel(s).")
    print("Next /ingest (or the scheduled ingest_tick) will re-cut full history into fresh threads.")


if __name__ == "__main__":
    asyncio.run(main(skip_confirm="--yes" in sys.argv, include_archive="--include-archive" in sys.argv))
