"""The ingest report posted to the logs channel (ADR-0013).

Rendering only - the composition root sends it. `shared.observability`
must not import `shared.gateway`: the scheduler imports observability,
and the gateway imports the scheduler, so calling `notify_logs` from
here would close a cycle.

It's a WhatsApp message, so it is built for a phone screen: a headline
anyone can read at a glance, one line per channel that did work, and the
classification/summarisation split that says where the money went.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from shared.config import settings
from shared.observability import context

if TYPE_CHECKING:  # pragma: no cover
    from shared.observability.ingest import ChannelTotals, IngestRunRecorder


def fmt_cost(cost: float | None) -> str:
    """Costs here are fractions of a cent, so the usual 2dp is all
    zeroes. 4 significant-ish digits keeps a $0.0041 run readable while
    still rendering a $12.40 day sensibly."""
    if cost is None:
        return "n/a"
    if cost >= 1:
        return f"${cost:,.2f}"
    return f"${cost:.4f}"


def fmt_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(tokens)


def fmt_duration(ms: int | None) -> str:
    if not ms:
        return "0s"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def _threads_phrase(channel: ChannelTotals) -> str:
    parts = []
    if channel.threads_created:
        parts.append(f"+{channel.threads_created} new")
    if channel.threads_updated:
        parts.append(f"~{channel.threads_updated} updated")
    if channel.threads_revived:
        parts.append(f"↑{channel.threads_revived} revived")
    if channel.chatter:
        parts.append(f"{channel.chatter} chatter")
    return " · ".join(parts) if parts else "no threads"


def render_ingest_report(run: IngestRunRecorder) -> str | None:
    """None when the run processed fewer than OBS_REPORT_MIN_MESSAGES -
    the tick fires every 2 minutes and nearly always finds nothing, so
    reporting unconditionally would bury the logs channel."""
    if run.messages < max(settings.obs_report_min_messages, 1):
        return None

    worked = [c for c in run.channels if c.messages]
    total_cost = sum(c.cost_usd for c in run.channels if c.cost_usd is not None)
    total_tokens = sum(c.tokens for c in run.channels)
    duration = sum(c.duration_ms for c in run.channels)

    lines = [
        f"📊 *Ingest #{run.run_id}* · {fmt_duration(duration)} · "
        f"{fmt_tokens(total_tokens)} tok · {fmt_cost(total_cost or None)}",
        "",
    ]

    for channel in worked:
        name = channel.channel_title or channel.channel_jid
        lines.append(f"*{name}* — {channel.messages} msg · {fmt_duration(channel.duration_ms)}")
        if channel.skipped_reason == "excluded":
            lines.append("  not ingested (logs channel)")
            continue
        detail = f"  {_threads_phrase(channel)}"
        if channel.calls:
            detail += f" · {fmt_tokens(channel.tokens)} tok · {fmt_cost(channel.cost_usd)}"
        lines.append(detail)
        if channel.unassigned:
            lines.append(f"  ⚠️ {channel.unassigned} unassigned → chatter")
        if channel.error:
            lines.append(f"  ❌ {channel.error}")

    phase_lines = []
    for phase, label in ((context.CLASSIFICATION, "classify "), (context.SUMMARISATION, "summarise")):
        totals = _sum_phase(run, phase)
        if totals.calls:
            phase_lines.append(
                f"{label}  {totals.calls} calls · {fmt_tokens(totals.tokens)} tok · "
                f"{fmt_cost(totals.cost_usd)}"
            )
    if phase_lines:
        lines.append("")
        lines.extend(phase_lines)

    return "\n".join(lines)


def _sum_phase(run: IngestRunRecorder, phase: str):
    from shared.observability.ingest import PhaseTotals

    totals = PhaseTotals()
    costs: list[float] = []
    for channel in run.channels:
        part = channel.phase(phase)
        totals.calls += part.calls
        totals.input_tokens += part.input_tokens
        totals.output_tokens += part.output_tokens
        if part.cost_usd is not None:
            costs.append(part.cost_usd)
    totals.cost_usd = sum(costs) if costs else None
    return totals
