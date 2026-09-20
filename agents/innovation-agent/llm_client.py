"""
Thin wrapper around the LLM API for the four lanes.

`generate_ideas` is the only entry point the lanes call. Ideas are
generated via a forced tool call against a JSON schema that mirrors
`Idea`/`Evidence`, so the response comes back as structured fields
instead of free text that then needs a second parsing pass.

Provider: OpenRouter (https://openrouter.ai), OpenAI-compatible endpoint,
pointed at openrouter/free — their auto-router that picks a free model
per-request, filtered for tool-calling support. Falls back to a stub if
OPENROUTER_API_KEY isn't set, so the demo runs without credentials.
"""
import json
import os
import time

from schemas import Idea, Origin, Evidence, EvidenceStatus

MODEL = "openrouter/free"
EMBEDDING_MODEL = "nvidia/nemotron-3-embed-1b:free"
BASE_URL = "https://openrouter.ai/api/v1"

_EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "claim": {"type": "string"},
        "source_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "IDs of the specific memory records (project/research entry IDs) that back "
                "this claim. Only use IDs actually given in the prompt context — never invent one."
            ),
        },
        "status": {
            "type": "string",
            "enum": ["fact", "observation", "unverified"],
            "description": (
                "'fact' only for something directly stated in a source record, 'observation' for "
                "a reasonable conclusion from it, 'unverified' if you're not confident it holds up."
            ),
        },
    },
    "required": ["claim", "source_ids", "status"],
}

_IDEA_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "statement": {"type": "string", "description": "What should be built, in 2-4 sentences."},
        "trigger_source_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "IDs from the provided context that this idea is grounded in. Empty list if "
                "genuinely unanchored (free lane)."
            ),
        },
        "evidence": {"type": "array", "items": _EVIDENCE_SCHEMA},
        "novelty_note": {
            "type": "string",
            "description": "Why this isn't something a zero-context prompt would already produce.",
        },
        "existing_leverage": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Existing ARIES components/capabilities this could reuse.",
        },
        "skill_match_present": {"type": "array", "items": {"type": "string"}},
        "skill_match_missing": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "why_now": {"type": "string"},
        "confidence_note": {
            "type": "string",
            "description": (
                "Honest hedge language matching how anchored this idea actually is — don't dress "
                "up a hunch as well-evidenced."
            ),
        },
    },
    "required": [
        "title", "statement", "trigger_source_ids", "evidence",
        "novelty_note", "risks", "why_now", "confidence_note",
    ],
}

IDEA_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_ideas",
        "description": "Submit one or more structured project idea pitches.",
        "parameters": {
            "type": "object",
            "properties": {"ideas": {"type": "array", "items": _IDEA_SCHEMA}},
            "required": ["ideas"],
        },
    },
}

VERIFY_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_verification",
        "description": "Judge whether a claim is actually supported by the given source records.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["fact", "observation", "unverified"],
                    "description": (
                        "'fact' if the records directly state this, 'observation' if it's a "
                        "reasonable conclusion from them, 'unverified' if the records don't "
                        "actually support the claim (including if they contradict it)."
                    ),
                }
            },
            "required": ["status"],
        },
    },
}


def generate_ideas(prompt: str, origin: Origin, n: int = 1) -> list[Idea]:
    if os.environ.get("OPENROUTER_API_KEY"):
        return _call_openrouter(prompt, origin, n)
    return _stub(prompt, origin, n)

def embed(texts: list[str]) -> list[list[float]] | None:
    """One embedding vector per input text, in order. Returns None (not
    an empty list) when there's no key, so callers can tell 'offline' apart
    from 'genuinely zero texts' and fall back to something else."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        return None
    from openai import OpenAI, RateLimitError

    client = OpenAI(base_url=BASE_URL, api_key=os.environ["OPENROUTER_API_KEY"])
    for attempt in range(3):
        try:
            resp = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
            return [d.embedding for d in resp.data]
        except RateLimitError:
            if attempt == 2:
                raise
            wait = 5 * (attempt + 1)
            print(f"  (embedding pool rate-limited, retrying in {wait}s...)")
            time.sleep(wait)


def verify_claim(claim: str, records: list[tuple[str, dict]]) -> EvidenceStatus:
    """Independently checks a claim against the actual memory records it cited —
    doesn't trust the generating call's own self-reported status."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        return EvidenceStatus.UNVERIFIED  # no key: conservative default, not a guess
    from openai import OpenAI

    client = OpenAI(base_url=BASE_URL, api_key=os.environ["OPENROUTER_API_KEY"])
    records_text = "\n".join(f"[{rid}]: {content}" for rid, content in records)
    prompt = (
        f"Claim: \"{claim}\"\n\nSource records cited as support:\n{records_text}\n\n"
        "Does the claim actually hold up against these records?"
    )
    resp = _call_with_retry(
        client, prompt, tools=[VERIFY_TOOL],
        tool_choice={"type": "function", "function": {"name": "submit_verification"}},
    )
    message = resp.choices[0].message
    if not message.tool_calls:
        return EvidenceStatus.UNVERIFIED  # model didn't judge — don't assume it checked out
    for call in message.tool_calls:
        if call.function.name == "submit_verification":
            args = json.loads(call.function.arguments)
            return EvidenceStatus(args.get("status", "unverified"))
    return EvidenceStatus.UNVERIFIED


def _call_with_retry(client, prompt: str, tools, tool_choice, max_tokens: int = 4000, max_retries: int = 3):
    from openai import RateLimitError

    for attempt in range(max_retries):
        try:
            return client.chat.completions.create(
                model=MODEL,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice=tool_choice,
                messages=[{"role": "user", "content": prompt}],
            )
        except RateLimitError:
            if attempt == max_retries - 1:
                raise
            wait = 5 * (attempt + 1)
            print(f"  (free pool rate-limited, retrying in {wait}s...)")
            time.sleep(wait)


def _call_openrouter(prompt: str, origin: Origin, n: int) -> list[Idea]:
    from openai import OpenAI

    client = OpenAI(base_url=BASE_URL, api_key=os.environ["OPENROUTER_API_KEY"])
    full_prompt = prompt + (
        f"\n\nSubmit exactly {n} idea(s)." if n > 1 else "\n\nSubmit exactly one idea."
    )
    resp = _call_with_retry(
        client, full_prompt, tools=[IDEA_TOOL],
        tool_choice={"type": "function", "function": {"name": "submit_ideas"}},
        max_tokens=12000,
    )
    message = resp.choices[0].message
    if not message.tool_calls:
        print(f"  (no tool call from {resp.model} for {origin.value} lane — got: {message.content!r})")
        return []

    for call in message.tool_calls:
        if call.function.name == "submit_ideas":
            args = json.loads(call.function.arguments)
            return [_parse_idea(d, origin) for d in args.get("ideas", [])]
    return []


def _parse_idea(data: dict, origin: Origin) -> Idea:
    evidence = [
        Evidence(
            claim=e.get("claim", ""),
            source_ids=e.get("source_ids", []),
            status=EvidenceStatus(e.get("status", "observation")),
        )
        for e in data.get("evidence", [])
    ]
    return Idea(
        title=data.get("title", ""),
        statement=data.get("statement", ""),
        origin=origin,
        trigger_source_ids=data.get("trigger_source_ids", []),
        evidence=evidence,
        novelty_note=data.get("novelty_note", ""),
        existing_leverage=data.get("existing_leverage", []),
        skill_match_present=data.get("skill_match_present", []),
        skill_match_missing=data.get("skill_match_missing", []),
        risks=data.get("risks", []),
        why_now=data.get("why_now", ""),
        confidence_note=data.get("confidence_note", ""),
    )


def _stub(prompt: str, origin: Origin, n: int) -> list[Idea]:
    return [
        Idea(
            title=f"[{origin.value}] sample idea {i + 1}",
            statement="Stubbed idea — set OPENROUTER_API_KEY to generate for real.",
            origin=origin,
            trigger_source_ids=[],
            evidence=[Evidence(claim="stub", source_ids=[], status=EvidenceStatus.OBSERVATION)],
            confidence_note="stub output, not a real pitch",
        )
        for i in range(n)
    ]