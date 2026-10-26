"""Make a fresh experiment look like a channel that went through `/setup`:
initiative page + channel row + channel page (via the real `register_channel`),
member pages, and member links for everyone `members.csv` gives a wiki_title.

Imports `shared/` - only ever imported from inside the runner subprocess.
"""

from __future__ import annotations

from shared.gateway.channels import link_member
from shared.gateway.onboarding import register_channel
from shared.wiki.interface import (
    PageExists,
    VaultClient,
    initiative_page_path,
    member_page_path,
    render_new_event_page,
    render_new_member_page,
    render_new_project_page,
)

from testbench.dataset import GroupData


async def _create_once(vault: VaultClient, path: str, content: str) -> None:
    try:
        await vault.create(path, content)
    except PageExists:
        pass


async def seed_group(vault: VaultClient, gd: GroupData, roster_titles: set[str]) -> None:
    g = gd.group
    if g.kind in ("project", "event"):
        render = render_new_project_page if g.kind == "project" else render_new_event_page
        await _create_once(
            vault,
            initiative_page_path(g.kind, g.initiative or g.title),
            render(g.initiative or g.title, lead=g.lead, brief=g.brief),
        )
    await register_channel(vault, g.jid, g.kind, g.channel_title, g.initiative)

    titles = {m.person.wiki_title for m in gd.messages if m.person.wiki_title}
    if g.lead in roster_titles:
        titles.add(g.lead)
    for title in sorted(titles):
        await _create_once(vault, member_page_path(title), render_new_member_page(title))
    linked: set[str] = set()
    for m in gd.messages:
        p = m.person
        if p.wiki_title and p.wa_identity not in linked:
            await link_member(p.wa_identity, p.wiki_title, None, "testbench")
            linked.add(p.wa_identity)
