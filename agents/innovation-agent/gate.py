"""
The shared gate every pitch passes through before a human ever sees it:
redundancy -> relevance -> novelty, in that order (cheapest and most
disqualifying first), with lane-aware thresholds baked into each check.
"""
from dataclasses import dataclass
import math

from schemas import Idea, Origin, PitchStatus, EvidenceStatus
from llm_client import embed


@dataclass
class GateResult:
    passed: bool
    status: PitchStatus
    notes: list[str]


def _title_word_overlap(a: str, b: str) -> float:
    # Offline fallback only — used when no OPENROUTER_API_KEY is set, so
    # redundancy checking degrades instead of silently doing nothing.
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def check_redundancy(idea: Idea, pitch_history: list[dict]) -> GateResult:
    rejected = [p for p in pitch_history if p.get("outcome") == "reject"]
    if not rejected:
        return GateResult(True, PitchStatus.PENDING, [])

    # One batched call — the idea's title plus every rejected title — not
    # one call per comparison, since that would multiply API calls by the
    # size of pitch history for every single idea checked.
    vectors = embed([idea.title] + [p["title"] for p in rejected])

    if vectors is None:
        # No key (offline/stub mode): degrade to the crude check rather
        # than skipping redundancy entirely.
        for past in rejected:
            if _title_word_overlap(idea.title, past["title"]) > 0.6:
                return GateResult(False, PitchStatus.GATED_OUT, [f"too similar to rejected pitch '{past['title']}'"])
        return GateResult(True, PitchStatus.PENDING, [])

    idea_vec, *rejected_vecs = vectors
    # TODO: 0.85 is an untested starting guess for cosine similarity on
    # short titles from this specific model — tune once there's enough
    # real rejected-pitch history to check it against actual duplicates
    # and actual non-duplicates.
    for past, vec in zip(rejected, rejected_vecs):
        if _cosine_similarity(idea_vec, vec) > 0.85:
            return GateResult(False, PitchStatus.GATED_OUT, [f"too similar to rejected pitch '{past['title']}'"])
    return GateResult(True, PitchStatus.PENDING, [])


def check_relevance(idea: Idea, org_snapshot: str) -> GateResult:
    if idea.origin == Origin.REMIDIAL:
        return GateResult(True, PitchStatus.PENDING, [])
    if not idea.statement.strip():
        return GateResult(False, PitchStatus.GATED_OUT, ["empty statement"])
    return GateResult(True, PitchStatus.PENDING, [])


def check_novelty(idea: Idea) -> GateResult:
    if idea.origin == Origin.FRONTIER:
        if len(idea.statement.split()) < 8:
            return GateResult(False, PitchStatus.GATED_OUT, ["too thin to be substantive"])
        return GateResult(True, PitchStatus.PENDING, [])
    # Everything else (remedial / bridged / events) is expected to cite
    # real evidence, since each of those lanes is anchored in a specific
    # memory query rather than unconstrained like free.
    if not idea.evidence:
        return GateResult(False, PitchStatus.GATED_OUT, [f"no evidence attached for a {idea.origin.value} pitch"])
    if all(e.status == EvidenceStatus.UNVERIFIED for e in idea.evidence):
        return GateResult(False, PitchStatus.GATED_OUT, ["all cited evidence failed independent verification"])
    return GateResult(True, PitchStatus.PENDING, [])


def run_gate(idea: Idea, pitch_history: list[dict], org_snapshot: str) -> GateResult:
    for check in (
        lambda: check_redundancy(idea, pitch_history),
        lambda: check_relevance(idea, org_snapshot),
        lambda: check_novelty(idea),
    ):
        result = check()
        if not result.passed:
            return result
    return GateResult(True, PitchStatus.PENDING, [])