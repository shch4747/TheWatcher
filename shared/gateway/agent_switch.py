"""Whether the Chat Agent answers mentions and replies. Off until a Bot
Admin sends `/toggle-agent`. Ingest and admin commands are unaffected."""
from __future__ import annotations

from shared.db import AppSetting, get_session

CHAT_AGENT_KEY = "chat_agent"


async def chat_agent_enabled() -> bool:
    async with get_session() as session:
        row = await session.get(AppSetting, CHAT_AGENT_KEY)
    return row is not None and row.value == "on"


async def set_chat_agent_enabled(enabled: bool) -> None:
    value = "on" if enabled else "off"
    async with get_session() as session:
        row = await session.get(AppSetting, CHAT_AGENT_KEY)
        if row is None:
            session.add(AppSetting(key=CHAT_AGENT_KEY, value=value))
        else:
            row.value = value
        await session.commit()


async def toggle_chat_agent() -> bool:
    enabled = not await chat_agent_enabled()
    await set_chat_agent_enabled(enabled)
    return enabled
