#!/usr/bin/env python3
"""Wipe all thread data and ingestion state so batch-cutting starts
fresh - for recovering from a bad batch (e.g. slop written before the
sanitizers in agents/wa_agent/interface.py existed) without touching
channel registration, initiative pages, or the channel index pages
themselves.

For every watched channel (shared.gateway.interface.list_channels):
  - deletes every thread page under `channels/<channel_dir>/` in the
    vault (Lapis if configured, else the local VAULT_ROOT directory) -
    NOT the channel's own `channels/<slug>.md` index page.
  - resets the channel's `cursor` to NULL.
  - marks every buffered message for that channel `processed = False`,
    so the next `/ingest` (or the scheduled ingest_tick) re-cuts the
    full history into fresh threads.
  - deletes any unconsumed project_agent Notices for that channel,
    since they'd otherwise point at thread slugs that no longer exist.

Usage:
    uv run python scripts/reset_threads.py           # asks to confirm
    uv run python scripts/reset_threads.py --yes      # no prompt
"""
from __future__ import annotations

import asyncio
import sys

from shared.db import Channel, MessageBuffer, Notice, get_session, init_db
from shared.gateway.interface import list_channels
from shared.wiki.interface import default_vault_client, slugify
from sqlalchemy import delete, select, update


async def main(skip_confirm: bool) -> None:
    await init_db()
    vault = default_vault_client()
    channels = await list_channels()

    if not channels:
        print("No watched channels - nothing to reset.")
        return

    print(f"About to reset {len(channels)} channel(s):")
    for c in channels:
        print(f"  - {c.jid}  kind={c.kind}  title={c.title or '-'}")

    if not skip_confirm:
        reply = input("Delete all their thread pages and reset cursors? [y/N] ").strip().lower()
        if reply != "y":
            print("Aborted.")
            return

    total_threads_deleted = 0
    for channel in channels:
        channel_dir = slugify(channel.title or channel.jid)
        paths = await vault.list(f"channels/{channel_dir}")
        for path in paths:
            await vault.delete(path)
        total_threads_deleted += len(paths)

        async with get_session() as session:
            row = await session.get(Channel, channel.id)
            assert row is not None
            row.cursor = None
            await session.execute(
                update(MessageBuffer).where(MessageBuffer.channel == channel.jid).values(processed=False)
            )
            await session.execute(delete(Notice).where(Notice.channel == channel.jid))
            await session.commit()

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
            f"{channel.jid}: deleted {len(paths)} thread page(s), reset cursor, "
            f"{unprocessed_count} message(s) now unprocessed"
        )

    print(f"\nDone. {total_threads_deleted} thread page(s) deleted across {len(channels)} channel(s).")
    print("Next /ingest (or the scheduled ingest_tick) will re-cut full history into fresh threads.")


if __name__ == "__main__":
    asyncio.run(main(skip_confirm="--yes" in sys.argv))
