#!/usr/bin/env python3
"""Bootstrap a Bot Admin (shared.db.BotAdmin). A fresh deploy has an
empty bot_admins table, and every admin command (`/setup`, `/unwatch`,
`/status`, `/link`) requires a row there - there is no other way in.

Usage:
    uv run python scripts/add_bot_admin.py <wa_identity>

`wa_identity` is the WhatsApp JID gowa reports for the sender, e.g.
"919876543210@s.whatsapp.net". If you don't know yours yet: send any
message from your phone to a group the bot is in (or DM the bot's
number), then check `messages_buffer.payload` in watcher.db (or the
container logs) for the `sender` field on that event.
"""
from __future__ import annotations

import asyncio
import sys

from shared.db import BotAdmin, get_session, init_db


async def main(wa_identity: str) -> None:
    await init_db()
    async with get_session() as session:
        existing = await session.get(BotAdmin, wa_identity)
        if existing is not None:
            print(f"{wa_identity} is already a Bot Admin.")
            return
        session.add(BotAdmin(wa_identity=wa_identity))
        await session.commit()
    print(f"Added {wa_identity} as a Bot Admin.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
