"""One handler per admin command. `handle_command` only parses and dispatches."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from shared.config import settings
from shared.gateway.channels import (
    admin_and_channel_counts,
    get_channel,
    is_bot_admin,
    link_member,
    list_channels,
)
from shared.gateway.commands import (
    ChannelsCommand,
    Command,
    HealthCommand,
    IngestCommand,
    LinkCommand,
    SetupCommand,
    StatusCommand,
    UnwatchCommand,
    parse_command,
)
from shared.gateway.onboarding import (
    channel_status_line,
    continue_setup_session,
    setup,
    unwatch,
)
from shared.gateway.runtime import GatewayRuntime
from shared.scheduler.interface import due_jobs, ledger_tail, registered_jobs, run_job
from shared.wiki.interface import LapisClient, VaultClient, check_vault_connection, default_vault_client

IST = timezone(timedelta(hours=5, minutes=30))


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
    lines.append(f"mentor model: {settings.mentor_model_name}")

    admin_count, channel_count = await admin_and_channel_counts()
    due = await due_jobs()
    lines.append(f"bot admins: {admin_count}  watched channels: {channel_count}")
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


async def link(cmd: LinkCommand, requested_by: str) -> str:
    if not await is_bot_admin(requested_by):
        return "Only Bot Admins can /link."
    member_title = cmd.member_ref.strip("[]")
    await link_member(cmd.sender_ref, member_title, linked_by=requested_by)
    return f"Linked {cmd.sender_ref} to [[{member_title}]]."


async def handle_command(
    channel_jid: str,
    sender: str,
    text: str,
    runtime: GatewayRuntime,
    vault: VaultClient | None = None,
) -> str | None:
    """Route a parsed command to its handler. `vault` defaults to
    `default_vault_client()`; pass a real client in tests."""
    cmd: Command | None = parse_command(text)
    vault = vault or default_vault_client()

    if cmd is None:
        return await continue_setup_session(channel_jid, text, vault)
    if isinstance(cmd, SetupCommand):
        return await setup(channel_jid, sender, cmd, vault)
    if isinstance(cmd, UnwatchCommand):
        return await unwatch(channel_jid, sender, target_jid=cmd.jid)
    if isinstance(cmd, StatusCommand):
        return await status(channel_jid)
    if isinstance(cmd, LinkCommand):
        return await link(cmd, sender)
    if isinstance(cmd, HealthCommand):
        return await health(runtime)
    if isinstance(cmd, ChannelsCommand):
        return await channels_report(sender, runtime)
    if isinstance(cmd, IngestCommand):
        return await trigger_ingest(sender)
    raise AssertionError(f"unhandled command type: {cmd!r}")
