"""
The evidence-verification loop: for every claim an idea cites, look up
the actual memory records behind it and independently check whether the
claim holds up — never trust the generating call's own self-reported
status. An idea's evidence should reflect what's actually in memory, not
what the model that wrote the idea asserted about itself.

Two cheap short-circuits happen before any LLM call, since they need no
judgment at all:
  - a cited source_id that doesn't exist in memory -> unverified,
    immediately (this is exactly the "invented a citation" case)
  - no source_ids cited at all -> unverified, immediately (expected and
    fine for the free lane, which doesn't require evidence)
Only a claim that cites IDs which actually exist gets the more expensive
step: an independent LLM judgment of whether the records really support it.
"""
from memory_interface import MemoryInterface
from schemas import Idea, EvidenceStatus
from llm_client import verify_claim


def verify_idea(idea: Idea, memory: MemoryInterface) -> Idea:
    for ev in idea.evidence:
        if not ev.source_ids:
            ev.status = EvidenceStatus.UNVERIFIED
            continue

        records = []
        missing = []
        for source_id in ev.source_ids:
            record = memory.get_record(source_id)
            if record is None:
                missing.append(source_id)
            else:
                records.append((source_id, record.get("content") or record.get("_body", "")))

        if missing:
            # Cited something that doesn't exist — automatic demotion,
            # no need to spend a call finding that out.
            ev.status = EvidenceStatus.UNVERIFIED
            continue

        ev.status = verify_claim(ev.claim, records)

    return idea