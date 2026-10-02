"""Thread lifecycle jobs: daily stale/archive pass and the Sunday nudge."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from shared.config import settings
from shared.gateway.interface import ChannelInfo, get_channel_by_kind, list_channels
from shared.gateway.interface import send as gateway_send
from shared.wiki.interface import ThreadStore, VaultClient


def lapis_link(path: str) -> str:
    return f"{settings.lapis_base_url}/vault/{settings.lapis_vault_id}/file/{path}"


async def run_lifecycle_for_channel(
    vault: VaultClient, channel: ChannelInfo, now: datetime | None = None
) -> dict[str, list[str]]:
    store = ThreadStore(vault, channel)
    stale = await store.mark_stale(now or datetime.now(UTC), timedelta(days=settings.thread_stale_days))
    archived = await store.archive_ended()
    if stale or archived:
        await store.reindex()
    return {"stale": stale, "archived": archived}


async def sunday_stale_nudge(vault: VaultClient, now: datetime | None = None) -> str | None:
    now = now or datetime.now(UTC)
    coordis = await get_channel_by_kind("coordis")
    if coordis is None:
        return None

    stale_paths: list[str] = []
    silent_paths: list[str] = []
    silent_after = timedelta(days=settings.thread_silent_days)
    for channel in await list_channels():
        for thread in await ThreadStore(vault, channel).threads(("active", "stale", "ended")):
            if thread.state == "stale":
                stale_paths.append(thread.path)
            if thread.last_message_at is not None and now - thread.last_message_at >= silent_after:
                silent_paths.append(thread.path)

    links = "\n".join(f"- {lapis_link(p)}" for p in stale_paths[:10])
    text = (
        f"There are {len(stale_paths)} stale threads, {len(silent_paths)} of them with no "
        f"messages in the last {int(settings.thread_silent_days)} days — please mark ended ones."
    )
    if links:
        text += f"\n{links}"

    await gateway_send(coordis.jid, text)
    return text
