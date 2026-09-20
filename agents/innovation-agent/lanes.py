"""
The four ways the Innovation Agent notices something worth pitching.
Each function takes memory, queries it, and returns a list of Idea
drafts — none of them write anything or decide what a human sees. The
gate and pipeline own that.
"""
from schemas import Idea, Origin
from memory_interface import MemoryInterface
from llm_client import generate_ideas


def _feedback_context(memory: MemoryInterface, lane: str) -> str:
    """Turns recent human verdicts on this lane's past pitches into a
    short block a prompt can append. Returns "" on cold start (no
    reviewed feedback yet for this lane) — every call site below just
    tacks this onto its prompt string, so an empty result is a no-op,
    not a special case callers need to handle."""
    liked = memory.get_recent_feedback(lane, limit=3, verdict="up")
    disliked = memory.get_recent_feedback(lane, limit=3, verdict="down")
    if not liked and not disliked:
        return ""

    lines = ["\n\nPast human feedback on ideas from this lane:"]
    for item in liked:
        lines.append(f"- LIKED: {item['idea_summary']} — {item.get('reason') or 'no reason given'}")
    for item in disliked:
        lines.append(f"- REJECTED: {item['idea_summary']} — {item.get('reason') or 'no reason given'}")
    lines.append(
        "Use this to calibrate what kind of ideas land well with this audience. "
        "Don't propose ideas that closely repeat a REJECTED pattern."
    )
    return "\n".join(lines)


def grounded_on_closure(memory: MemoryInterface, project_id: str) -> list[Idea]:
    """Fires when a project closes. Checks its failure cause against
    other closed projects' causes — a real repeat is worth a pitch."""
    cause = memory.get_failure_cause(project_id)
    if not cause:
        return []
    all_causes = memory.get_all_closed_project_causes()
    matches = [pid for pid, c in all_causes.items() if c == cause and pid != project_id]
    if not matches:
        return []
    prompt = (
        f"ARIES projects {matches + [project_id]} all failed for the same reason: '{cause}'. "
        "Propose one piece of infrastructure or process that would prevent this recurring a "
        "fourth time. Do not propose merging or fixing the existing projects — that is handled "
        "elsewhere. Only propose something new."
    ) + _feedback_context(memory, "grounded")
    return generate_ideas(prompt, Origin.GROUNDED)


def grounded_weekly_sweep(memory: MemoryInterface) -> list[Idea]:
    """Absence-shaped patterns that have no trigger event: a capability
    used once and gone quiet, a plain gap in what's been attempted."""
    usage = memory.get_capability_usage()
    idle = {cap: ids[0] for cap, ids in usage.items() if len(ids) == 1}
    if not idle:
        return []
    prompt = (
        f"ARIES has these capabilities used only once and otherwise idle, each with the "
        f"project ID that used it: {idle}. Propose a new application for one of them, and cite "
        "that project ID as evidence for the capability's existence."
    ) + _feedback_context(memory, "grounded")
    return generate_ideas(prompt, Origin.GROUNDED)


def bridged_on_research(memory: MemoryInterface, entry_id: str) -> list[Idea]:
    """Fires when a research entry gets a deep-dive. Deliberately looks
    PAST the obvious connection that earned it the deep-dive, at
    capabilities the deep-dive process didn't already link it to."""
    entry = memory.get_research_entry(entry_id)
    if not entry:
        return []
    far_capabilities = memory.get_capabilities_far_from(entry.get("topic", ""))
    prompt = (
        f"New research: '{entry.get('title')}'. "
        f"Here are ARIES capabilities NOT obviously related to it: {far_capabilities}. "
        "Is there a real connection anyway — same underlying mechanism, different surface "
        "domain? Only propose something if the mechanism genuinely transfers, not just if the "
        "topics sound similar."
    ) + _feedback_context(memory, "bridged")
    return generate_ideas(prompt, Origin.BRIDGED)


def free_weekly(memory: MemoryInterface) -> list[Idea]:
    """No anchor at all. Oversampled; the gate and human review do the
    filtering, not this step."""
    snapshot = memory.get_org_snapshot()
    digest = memory.get_light_research_digest(since="")
    titles = [d["title"] for d in digest]
    prompt = (
        f"ARIES today: {snapshot}\n"
        f"Recent broad AI developments (not necessarily related to ARIES): {titles}\n"
        "Propose an idea that would surprise a technically sharp ARIES member — something "
        "whose connection to ARIES isn't obvious until explained. No requirement that it ties "
        "to anything specific above."
    ) + _feedback_context(memory, "free")
    return generate_ideas(prompt, Origin.FREE, n=15)


def events_weekly_sweep(memory: MemoryInterface) -> list[Idea]:
    """Fires on the same weekly cadence as the other sweeps. Looks at
    event feedback history for two things at once: formats that scored
    well and haven't been repeated in a while, and patterns in what
    scored poorly, so a new pitch doesn't repeat a known miss."""
    history = memory.get_event_history()
    if not history:
        return []
    prompt = (
        f"ARIES's past events, workshops, and hackathons, each with an ID, attendance, and "
        f"feedback: {history}. Propose one new event, workshop, or hackathon idea grounded in "
        "what has actually engaged this audience before. Prefer formats similar to well-received "
        "past events over untested ones, and avoid repeating a pattern that scored poorly. Cite "
        "the specific past event ID(s) informing this pitch as evidence."
    ) + _feedback_context(memory, "events")
    return generate_ideas(prompt, Origin.EVENTS)