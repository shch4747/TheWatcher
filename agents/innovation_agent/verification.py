"""
The evidence-verification loop: for every claim an idea cites, look up
the actual records behind it and independently check whether the claim
holds up — never trust the generating call's own self-reported status.
"""
from typing import Callable

from schemas import Idea, EvidenceStatus
from llm_client import verify_claim


def verify_idea(idea: Idea, get_record: Callable[[str], dict | None]) -> Idea:
    for ev in idea.evidence:
        if not ev.source_ids:
            ev.status = EvidenceStatus.UNVERIFIED
            continue

        records = []
        missing = []
        for source_id in ev.source_ids:
            record = get_record(source_id)
            if record is None:
                missing.append(source_id)
            else:
                records.append((source_id, record.get("content") or record.get("_body", "")))

        if missing:
            ev.status = EvidenceStatus.UNVERIFIED
            continue

        ev.status = verify_claim(ev.claim, records)

    return idea