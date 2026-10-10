"""Reaction hook handling red-cross (❌) reactions to undo auto-linked members."""
from __future__ import annotations

import logging

from shared.gateway.auto_links import undo_notice
from shared.gateway.interface import is_bot_admin, send

CROSS = {"\u274c", "\u274c\ufe0f"}


async def handle_undo_reaction(reactor: str, message_id: str | None, emoji: str | None) -> str | None:
    if not message_id or emoji not in CROSS:
        return None
    if not await is_bot_admin(reactor):
        return None
    result = await undo_notice(message_id, reactor)
    if result is None:
        return None
    text = f"Undone: removed {result.removed} link(s) to [[{result.member_title}]]."
    if result.kept > 0:
        text += f" Kept {result.kept} that were changed since."
    text += " /setup-members will not auto-link this again; use /link if it was a mistake."
    try:
        await send(result.channel, text)
    except Exception:
        logging.getLogger(__name__).warning("Failed to send undo confirmation")
    return text
