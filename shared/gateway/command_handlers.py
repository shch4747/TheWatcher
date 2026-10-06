"""One handler per admin command. `handle_command` only parses and dispatches."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from shared.cms.interface import MEMBER_MATCH_THRESHOLD, default_cms_client, rank_member
from shared.config import settings
from shared.gateway.agent_switch import chat_agent_enabled, toggle_chat_agent
from shared.gateway.channels import (
    admin_and_channel_counts,
    get_channel,
    is_bot_admin,
    link_member,
    list_channels,
    resolve_sender,
)
from shared.gateway.commands import (
    ChannelsCommand,
    Command,
    HealthCommand,
    HelpCommand,
    IngestCommand,
    LinkCommand,
    MemberCommand,
    SetupCommand,
    SetupMembersCommand,
    StatusCommand,
    ToggleAgentCommand,
    UnwatchCommand,
    mentioned_jid,
    parse_command,
)
from shared.gateway.onboarding import (
    channel_status_line,
    continue_setup_session,
    setup,
    unwatch,
)
from shared.gateway.runtime import GatewayRuntime
from shared.gateway.types import GroupParticipant
from shared.scheduler.interface import due_jobs, ledger_tail, registered_jobs, run_job
from shared.wiki.interface import (
    LapisClient,
    PageNotFound,
    VaultClient,
    check_vault_connection,
    default_vault_client,
    member_page_path,
    parse_item_line,
    parse_page_lenient,
)

IST = timezone(timedelta(hours=5, minutes=30))

_HELP = "\n".join(
    [
        "*Commands*",
        "/setup [project|event|coordis|exes|research|all|other|logs] [Title] — watch this group",
        "/channels — list watched channels",
        "/unwatch [jid] — stop watching this group, or another by jid",
        "/status — this channel's kind, initiative, and cursor",
        "/ingest — process unprocessed messages now, ignoring batch limits",
        "/health — connectivity and the last outcome of each job",
        "/setup-members — match this group's members to the ARIES roster",
        "/link @member [[Member Title]] — link the mentioned person to a member",
        "/member @person — what this person is working on, from the wiki",
        "/toggle-agent — turn chat replies on or off",
    ]
)


def _isoformat(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")


async def status(channel_jid: str) -> str:
    return channel_status_line(await get_channel(channel_jid))


async def channels_report(requested_by: str, runtime: GatewayRuntime) -> str:
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /channels."
    channels = await list_channels()
    if not channels:
        return "No channels are being watched."
    names = await runtime.group_name_map()
    lines = [f"*Watched channels* ({len(channels)})"]
    for channel in channels:
        label = f"{names[channel.jid]} ({channel.jid})" if channel.jid in names else channel.jid
        title = f" title={channel.title}" if channel.title else ""
        initiative = f" initiative={channel.initiative}" if channel.initiative else ""
        lines.append(f"- {label}  kind={channel.kind}{title}{initiative}")
    return "\n".join(lines)


async def trigger_ingest(requested_by: str) -> str:
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /ingest."
    try:
        result = await run_job("ingest_now")
    except KeyError:
        return "ingest_now isn't registered - is the app running via main.py (not just uvicorn)?"
    if result.outcome == "success":
        return "Ingestion triggered ✅ (ignored batch thresholds - cut whatever was unprocessed)"
    return f"Ingestion failed: {result.error}"


async def toggle_agent(requested_by: str) -> str:
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /toggle-agent."
    enabled = await toggle_chat_agent()
    return "Chat agent is on." if enabled else "Chat agent is off."


async def help_text() -> str:
    state = "on" if await chat_agent_enabled() else "off"
    return f"{_HELP}\n\nChat agent is {state}."


async def health(runtime: GatewayRuntime) -> str:
    lines = ["*Watcher health*"]

    gowa_result = await runtime.check_gowa_connection()
    lines.append(f"gowa: {'✅ ok' if gowa_result.get('ok') else '❌ ' + str(gowa_result.get('error'))}")

    vault = default_vault_client()
    backend = getattr(vault, "inner", vault)
    vault_kind = "Lapis (live)" if isinstance(backend, LapisClient) else "local directory"
    vault_result = await check_vault_connection(vault)
    vault_status = "✅ ok" if vault_result.get("ok") else f"❌ {vault_result.get('error')}"
    lines.append(f"vault: {vault_status} ({vault_kind})")

    if settings.jev_api_key:
        decision_line = f"decision model: Jev ({settings.jev_base_url or 'hosted'})"
    else:
        decision_line = "decision model: Worker-backed (no JEV_API_KEY)"
    lines.append(decision_line)
    lines.append(f"worker model: {settings.worker_model_name}")
    if settings.assignment_mode == "jev":
        lines.append(
            f"assignment: Jev ({settings.openrouter_jev_model}); "
            f"new threads → {settings.worker_model_name}"
        )
    else:
        lines.append(
            f"assignment model: {settings.ingest_model_name or settings.assignment_model_name}"
        )
    lines.append(f"mentor model: {settings.mentor_model_name}")

    admin_count, channel_count = await admin_and_channel_counts()
    due = await due_jobs()
    lines.append(f"bot admins: {admin_count}  watched channels: {channel_count}")
    lines.append(f"chat agent: {'on' if await chat_agent_enabled() else 'off'}")
    lines.append(f"jobs due: {len(due)} {due if due else ''}".strip())
    for health_line in runtime.hooks.health_lines:
        try:
            lines.append(await health_line())
        except Exception as exc:  # noqa: BLE001 - /health reports, never fails
            lines.append(f"❌ {exc}")

    for job_name in registered_jobs():
        last = await ledger_tail(job_name, limit=1)
        if not last:
            lines.append(f"{job_name}: no runs yet")
            continue
        run = last[0]
        if run.outcome == "success":
            lines.append(f"{job_name}: ✅ last ran {_isoformat(run.finished_at)}")
        elif run.outcome == "failure":
            lines.append(f"{job_name}: ❌ failed {_isoformat(run.finished_at)} - {run.error}")
        else:
            lines.append(f"{job_name}: ⏳ started {_isoformat(run.started_at)}, still running")

    return "\n".join(lines)


async def link(cmd: LinkCommand, requested_by: str, mentions: list[str] | None = None) -> str:
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /link."
    wa_identity = mentioned_jid(cmd.mention, mentions or [])
    if wa_identity is None:
        return "Mention the person in this message, then [[Member Title]]. A typed JID is not enough."
    member_title = cmd.member_ref.strip("[]")
    cms_id = None
    try:
        member = await default_cms_client().get_member(member_title)
    except Exception:  # noqa: BLE001 - a manual link still stands if the roster is down
        member = None
    if member is not None:
        cms_id = member.cms_id
        member_title = member.title
    await link_member(wa_identity, member_title, cms_id, linked_by=requested_by)
    return f"Linked @{cmd.mention} to [[{member_title}]]."


_MEMBER_TASK_LIMIT = 10
_MEMBER_ABOUT_CHARS = 600


def _about_text(content: str) -> str | None:
    """The About section of a member page, or None if it can't be read."""
    try:
        page, _error = parse_page_lenient(content)
    except Exception:  # noqa: BLE001 - a hand-edited page must not break the command
        return None
    section = page.section("About")
    body = section.body.strip() if section is not None else ""
    return body[:_MEMBER_ABOUT_CHARS] or None


def _open_task_line(text: str) -> str | None:
    """One search hit as a reply line; None for a finished task."""
    item = parse_item_line(text)
    if item is None:
        return f"- {text}"
    if item.checked:
        return None
    due = item.fields.get("due")
    return f"- {item.text}" + (f" (due {due})" if due else "")


async def member_report(
    cmd: MemberCommand, requested_by: str, vault: VaultClient, mentions: list[str] | None = None
) -> str:
    """`/member @person`: their About text and open tasks, read from the wiki."""
    wa_identity = mentioned_jid(cmd.mention, mentions or [])
    if wa_identity is None:
        return "Mention the person in this message: /member @name"
    member = await resolve_sender(wa_identity)
    if member is None:
        return "That person isn't linked to a member yet. An admin can /link them first."
    title = member.member_title

    about: str | None
    try:
        page = await vault.read(member_page_path(title))
    except PageNotFound:
        about = None
    else:
        about = _about_text(page.content)

    try:
        hits = await vault.search(f"owner:: [[{title}]]", limit=_MEMBER_TASK_LIMIT + 1)
    except Exception as exc:  # noqa: BLE001 - the person asking needs the failure, not a traceback
        return f"Couldn't search the wiki for {title}'s tasks: {exc}"
    tasks = [
        line for hit in hits[:_MEMBER_TASK_LIMIT] if (line := _open_task_line(hit.text)) is not None
    ]

    lines = [f"*{title}*", about or "(no About text on their wiki page yet)", "", "*Open tasks*"]
    lines += tasks or ["(none found)"]
    if len(hits) > _MEMBER_TASK_LIMIT:
        lines.append("...there may be more, only the first matches are shown")
    return "\n".join(lines)


def _participant_label(person: GroupParticipant) -> str:
    if person.display_name:
        return person.display_name
    phone = (person.phone_number or "").split("@", 1)[0]
    return phone or "unnamed"


async def _link_identity(wa_identity: str, title: str, cms_id: str | None, linked_by: str) -> None:
    if await resolve_sender(wa_identity) is None:
        await link_member(wa_identity, title, cms_id, linked_by=linked_by)


async def setup_members(channel_jid: str, requested_by: str, runtime: GatewayRuntime) -> str:
    """Match every group participant to the ARIES roster. A confident
    fuzzy match is linked immediately. Everyone else is listed so an
    admin can /link them by mentioning them."""
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /setup-members."
    try:
        participants = await runtime.group_participants(channel_jid)
    except Exception as exc:  # noqa: BLE001 - the admin needs the failure, not a traceback
        return f"Couldn't list this group's members: {exc}"
    try:
        roster = await default_cms_client().list_members()
    except Exception as exc:  # noqa: BLE001
        return f"Couldn't read the ARIES member list: {exc}"

    bot = await runtime.bot_jid()
    linked: list[str] = []
    unsure: list[str] = []
    already = 0
    for person in participants:
        identities = [person.jid]
        if person.lid and person.lid not in identities:
            identities.append(person.lid)
        if bot and bot in identities:
            continue
        existing = None
        for identity in identities:
            existing = await resolve_sender(identity)
            if existing is not None:
                break
        if existing is not None:
            for identity in identities:
                await _link_identity(identity, existing.member_title, existing.cms_member_id, requested_by)
            already += 1
            continue
        ranked = rank_member(person.display_name or "", roster)
        if ranked is not None and ranked[1] >= MEMBER_MATCH_THRESHOLD:
            member, _score = ranked
            for identity in identities:
                await link_member(identity, member.title, member.cms_id, linked_by=requested_by)
            linked.append(f"- {_participant_label(person)} → [[{member.title}]]")
            continue
        hint = f" (nearest [[{ranked[0].title}]])" if ranked is not None else ""
        unsure.append(f"- {_participant_label(person)}{hint}")

    lines = [
        "*Members*",
        f"Linked {len(linked)}, already linked {already}, {len(unsure)} need a manual /link.",
    ]
    if linked:
        lines += ["", "Linked:", *linked]
    if unsure:
        titles = ", ".join(member.title for member in roster) or "(roster is empty)"
        lines += [
            "",
            "Mention each of these, then /link @them [[Member Title]]:",
            *unsure,
            "",
            f"Member titles: {titles}",
        ]
    return "\n".join(lines)


async def handle_command(
    channel_jid: str,
    sender: str,
    text: str,
    runtime: GatewayRuntime,
    vault: VaultClient | None = None,
    mentions: list[str] | None = None,
) -> str | None:
    """Route a parsed command to its handler. `vault` defaults to
    `default_vault_client()`; pass a real client in tests."""
    cmd: Command | None = parse_command(text)
    vault = vault or default_vault_client()

    if cmd is None:
        return await continue_setup_session(
            channel_jid, text, vault, on_watching=runtime.on_channel_watched, mentions=mentions
        )
    if isinstance(cmd, SetupMembersCommand):
        return await setup_members(channel_jid, sender, runtime)
    if isinstance(cmd, SetupCommand):
        return await setup(channel_jid, sender, cmd, vault, on_watching=runtime.on_channel_watched)
    if isinstance(cmd, UnwatchCommand):
        return await unwatch(channel_jid, sender, target_jid=cmd.jid)
    if isinstance(cmd, StatusCommand):
        return await status(channel_jid)
    if isinstance(cmd, LinkCommand):
        return await link(cmd, sender, mentions)
    if isinstance(cmd, MemberCommand):
        return await member_report(cmd, sender, vault, mentions)
    if isinstance(cmd, HealthCommand):
        return await health(runtime)
    if isinstance(cmd, ChannelsCommand):
        return await channels_report(sender, runtime)
    if isinstance(cmd, IngestCommand):
        return await trigger_ingest(sender)
    if isinstance(cmd, ToggleAgentCommand):
        return await toggle_agent(sender)
    if isinstance(cmd, HelpCommand):
        return await help_text()
    raise AssertionError(f"unhandled command type: {cmd!r}")
