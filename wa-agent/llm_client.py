"""
The classify step: a thread of WhatsApp messages -> a list of typed Signals.

Mirrors innovation-agent/llm_client.py exactly in spirit:
  - structured output via a forced tool call against a JSON schema, so the
    model returns typed fields instead of prose to re-parse;
  - OpenRouter (OpenAI-compatible), pointed at Kimi K2.6;
  - a heuristic STUB fallback when OPENROUTER_API_KEY is unset, so the whole
    pipeline runs and is testable with no credentials and no network.

Model: Kimi K2.6 (moonshotai/kimi-k2.6) via OpenRouter — strong agentic /
tool-calling model, which is what the forced-tool-call classification needs.
Override with WA_LLM_MODEL if you point at Moonshot's own endpoint instead.

`classify_thread` is the only entry point the pipeline calls.
"""
import json
import os
import uuid

from schemas import Thread, Signal, SignalType

MODEL = os.environ.get("WA_LLM_MODEL", "moonshotai/kimi-k2.6")
BASE_URL = os.environ.get("WA_LLM_BASE_URL", "https://openrouter.ai/api/v1")

_VALID_TYPES = [t.value for t in SignalType]

_SIGNAL_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": _VALID_TYPES,
                 "description": "What this message/exchange is."},
        "summary": {"type": "string", "description": "One line, human-readable."},
        "source_message_ids": {"type": "array", "items": {"type": "string"},
                               "description": "IDs of the messages this signal came from. "
                                              "Only use ids present in the transcript."},
        "project_ref": {"type": "string",
                        "description": "Project slug/name if the signal clearly names one, else empty."},
        "confidence": {"type": "number"},
        "needs_clarification": {"type": "boolean",
                                "description": "True if the meaning is ambiguous and a human "
                                               "should be asked in-chat before acting."},
        "clarification_question": {"type": "string"},
        "payload": {"type": "object",
                    "description": "Type-specific extras, e.g. {url, authors} for a research_paper."},
    },
    "required": ["type", "summary", "source_message_ids", "confidence"],
}

CLASSIFY_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_signals",
        "description": "Submit the meaningful signals extracted from a WhatsApp thread. "
                       "Emit nothing for pure chatter.",
        "parameters": {
            "type": "object",
            "properties": {"signals": {"type": "array", "items": _SIGNAL_SCHEMA}},
            "required": ["signals"],
        },
    },
}

_SYSTEM = (
    "You extract structured signals from an ARIES (AI/ML club) WhatsApp thread. "
    "Route intent by type: research_paper (a shared paper/arxiv/link to research) -> Research Agent; "
    "project_idea / event_idea -> Innovation Agent; project_update -> Project Agent; "
    "event_info (a concrete event with date/venue), task, decision, announcement, question -> wiki. "
    "Ignore banter (type=noise). Only cite message ids that appear in the transcript; never invent one."
)


def classify_thread(thread: Thread) -> list[Signal]:
    if os.environ.get("OPENROUTER_API_KEY"):
        return _call_openrouter(thread)
    return _stub_classify(thread)


def _call_openrouter(thread: Thread) -> list[Signal]:
    from openai import OpenAI

    client = OpenAI(base_url=BASE_URL, api_key=os.environ["OPENROUTER_API_KEY"])
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=4000,
        tools=[CLASSIFY_TOOL],
        tool_choice={"type": "function", "function": {"name": "submit_signals"}},
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"Thread (group {thread.group_id}):\n{thread.as_transcript()}"},
        ],
    )
    message = resp.choices[0].message
    if not message.tool_calls:
        # TODO: fall back to parsing message.content, like innovation-agent's noted fix.
        print(f"  (no tool call from {resp.model}; got: {message.content!r})")
        return []
    for call in message.tool_calls:
        if call.function.name == "submit_signals":
            args = json.loads(call.function.arguments)
            return [_parse_signal(s, thread) for s in args.get("signals", [])]
    return []


def _parse_signal(data: dict, thread: Thread) -> Signal:
    try:
        stype = SignalType(data.get("type", "noise"))
    except ValueError:
        stype = SignalType.NOISE
    return Signal(
        id=str(uuid.uuid4())[:8],
        type=stype,
        summary=data.get("summary", ""),
        source_message_ids=data.get("source_message_ids", []),
        group_id=thread.group_id,
        project_ref=data.get("project_ref") or None,
        confidence=float(data.get("confidence", 0.0)),
        needs_clarification=bool(data.get("needs_clarification", False)),
        clarification_question=data.get("clarification_question") or None,
        payload=data.get("payload", {}) or {},
    )


# --------------------------------------------------------------------------
# Heuristic stub — keeps the pipeline runnable & deterministic without a key.
# Good enough to exercise routing in tests; NOT the real classifier.
# --------------------------------------------------------------------------

def _stub_classify(thread: Thread) -> list[Signal]:
    signals: list[Signal] = []
    for m in thread.messages:
        stype = _guess_type(m.text)
        if stype == SignalType.NOISE:
            continue
        signals.append(Signal(
            id=str(uuid.uuid4())[:8],
            type=stype,
            summary=m.text[:120],
            source_message_ids=[m.id],
            group_id=thread.group_id,
            confidence=0.4,
            needs_clarification=m.needs_human_tldr,
            clarification_question=(
                f"Could someone summarise the voice note from {m.sender}?"
                if m.needs_human_tldr else None
            ),
            payload=_extract_payload(stype, m.text),
        ))
    return signals


def _guess_type(text: str) -> SignalType:
    t = text.lower()
    if "arxiv" in t or "alphaxiv" in t or "paper" in t or ("http" in t and "github" not in t):
        return SignalType.RESEARCH_PAPER
    if any(k in t for k in ("idea", "we should build", "propose", "what if we", "hackathon idea")):
        return SignalType.PROJECT_IDEA
    if any(k in t for k in ("event", "talk", "workshop", "session on", "venue", "rsvp")):
        return SignalType.EVENT_INFO
    if any(k in t for k in ("update:", "progress", "merged", "deployed", "blocked", "shipped", "pushed")):
        return SignalType.PROJECT_UPDATE
    if any(k in t for k in ("todo", "task", "assign", "by tomorrow", "deadline", "due ")):
        return SignalType.TASK
    if "?" in t:
        return SignalType.QUESTION
    if any(k in t for k in ("announcing", "announcement", "reminder:", "note:")):
        return SignalType.ANNOUNCEMENT
    if text.startswith("[voice note"):
        return SignalType.QUESTION  # unresolved voice -> surfaced for a human
    return SignalType.NOISE


def _extract_payload(stype: SignalType, text: str) -> dict:
    if stype == SignalType.RESEARCH_PAPER:
        for tok in text.split():
            if tok.startswith("http"):
                return {"url": tok}
    return {}


# --------------------------------------------------------------------------
# Outbound: summarise an agent's output into a WhatsApp-ready message for a
# specific group. Uses Kimi when a key is set, else a plain format.
# --------------------------------------------------------------------------

def summarise_notification(payload: str, source_agent: str,
                           group_name: str = "", group_context: str = "") -> str:
    if os.environ.get("OPENROUTER_API_KEY"):
        try:
            return _summarise_llm(payload, source_agent, group_name, group_context)
        except Exception as e:  # never let outbound formatting crash a send
            print(f"  (summarise fell back to plain: {type(e).__name__})")
    label = f"[{source_agent}]" if source_agent else ""
    return f"{label} {payload}".strip()


def _summarise_llm(payload, source_agent, group_name, group_context) -> str:
    from openai import OpenAI
    client = OpenAI(base_url=BASE_URL, api_key=os.environ["OPENROUTER_API_KEY"])
    sys = (
        "You turn an ARIES agent's output into ONE concise WhatsApp message for a "
        "specific group. Plain text, no markdown tables, a couple of short lines max, "
        "friendly and specific. Don't invent facts beyond the output given."
    )
    ctx = f"\nGroup: {group_name}\nWhat this group has been discussing:\n{group_context}\n" if group_name else ""
    resp = client.chat.completions.create(
        model=MODEL, max_tokens=400,
        messages=[
            {"role": "system", "content": sys},
            {"role": "user", "content": f"Agent: {source_agent}{ctx}\nOutput to share:\n{payload}"},
        ],
    )
    return resp.choices[0].message.content.strip()
