"""Wiki lookup tools and the guarded on-demand ingestion tool."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from shared.gateway.interface import ChannelInfo, is_bot_admin, list_channels
from shared.wiki.interface import PageNotFound, ThreadStore, VaultClient

MAX_READ_CHARS = 6000
SEARCH_LIMIT = 30
SUMMARY_CHARS = 160


def channel_ingest_tool(
    requester: str,
    current_channel_jid: str,
    run_ingestion: Callable[[str], Awaitable[str]],
) -> Callable[..., object]:
    """Build an admin-only tool that ingests a watched channel on demand."""

    async def ingest_channel(channel: str | None = None) -> str:
        """Ingest pending messages for a watched channel. Use a channel JID,
        exact title, or initiative name; omit it to use this WhatsApp channel.
        Only Bot Admins may run ingestion. It can be expensive, so use it only
        when a question depends on recent messages that are not in the wiki."""
        if not await is_bot_admin(requester):
            return "Only Bot Admins can run on-demand ingestion."

        channels = await list_channels()
        if channel is None or not channel.strip():
            matches = [item for item in channels if item.jid == current_channel_jid]
        else:
            query = channel.strip().casefold()
            matches = [
                item
                for item in channels
                if query in {
                    item.jid.casefold(),
                    (item.title or "").casefold(),
                    (item.initiative or "").casefold(),
                }
            ]
        if not matches:
            return "No watched channel matched that name or JID."
        if len(matches) > 1:
            options = ", ".join(
                f"{item.title or item.jid} ({item.jid})" for item in matches
            )
            return f"That channel name is ambiguous. Choose a JID: {options}"
        if matches[0].kind == "logs":
            return "The logs channel is excluded from ingestion."
        return await run_ingestion(matches[0].jid)

    return ingest_channel


async def list_threads(vault: VaultClient, channel: ChannelInfo | None) -> str:
    if channel is None:
        return "This WhatsApp group is not a watched channel, so it has no wiki threads."
    threads = await ThreadStore(vault, channel).threads(order="recent")
    if not threads:
        return "No active or stale threads in this channel."
    lines: list[str] = []
    for thread in threads:
        summary = " ".join(thread.summary.split())[:SUMMARY_CHARS]
        extra = f" — {summary}" if summary else ""
        last_message = (
            f" — last {thread.last_message_at:%Y-%m-%d}" if thread.last_message_at is not None else ""
        )
        lines.append(f"- {thread.path} | {thread.title} [{thread.state}]{last_message}{extra}")
    return "\n".join(lines)


async def read_page(vault: VaultClient, path: str) -> str:
    try:
        result = await vault.read(path)
    except PageNotFound:
        return f"No page at {path}."
    content = result.content
    if len(content) > MAX_READ_CHARS:
        return content[:MAX_READ_CHARS] + "\n…(truncated)"
    return content


async def search_wiki(vault: VaultClient, query: str, prefix: str = "") -> str:
    query = query.strip()
    if not query:
        return "Search query is empty."
    hits = await vault.search(query, path=prefix or None, limit=SEARCH_LIMIT, ignore_case=True, literal=True)
    if not hits:
        where = f" under {prefix}" if prefix else ""
        return f"No matches for {query!r}{where}."
    return "\n".join(f"{hit.path}:{hit.line}: {hit.text}" for hit in hits)


def read_only_wiki_tools(
    vault: VaultClient, channel: ChannelInfo | None
) -> Sequence[Callable[..., object]]:
    async def list_channel_threads() -> str:
        """List this channel's active and stale wiki threads (path, title, state, summary).
        Start here when the recent chat messages are not enough to answer."""
        return await list_threads(vault, channel)

    async def read_wiki_page(path: str) -> str:
        """Read a wiki page by vault path, e.g. channels/foo/hall-booking.md or people/aira.md."""
        return await read_page(vault, path)

    async def search_pages(query: str, prefix: str = "") -> str:
        """Search wiki page contents for a literal string. Optional prefix like 'channels/foo' or 'people'."""
        return await search_wiki(vault, query, prefix)

    return (list_channel_threads, read_wiki_page, search_pages)
