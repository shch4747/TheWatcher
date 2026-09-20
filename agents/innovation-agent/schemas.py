"""
Core data structures for the Innovation Agent.

Plain, serializable dataclasses on purpose — they should be writable
straight to Lapis (or any memory backend) as JSON/frontmatter without
a translation layer in between.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Origin(str, Enum):
    GROUNDED = "grounded"
    BRIDGED = "bridged"
    FREE = "free"
    EVENTS = "events"


class EvidenceStatus(str, Enum):
    FACT = "fact"
    OBSERVATION = "observation"
    UNVERIFIED = "unverified"


class PitchStatus(str, Enum):
    PENDING = "pending"        # waiting in the human-review queue
    GATED_OUT = "gated_out"    # killed by the shared gate, never shown to a human
    PURSUE = "pursue"
    DEFER = "defer"
    REJECT = "reject"


@dataclass
class Evidence:
    claim: str
    source_ids: list[str]
    status: EvidenceStatus = EvidenceStatus.OBSERVATION


@dataclass
class Idea:
    title: str
    statement: str
    origin: Origin
    trigger_source_ids: list[str]           # what in memory set this off
    evidence: list[Evidence] = field(default_factory=list)
    novelty_note: str = ""
    existing_leverage: list[str] = field(default_factory=list)
    skill_match_present: list[str] = field(default_factory=list)
    skill_match_missing: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    why_now: str = ""
    confidence_note: str = ""               # honest, lane-appropriate hedge language


@dataclass
class Pitch:
    id: str
    idea: Idea
    created_at: datetime
    status: PitchStatus = PitchStatus.PENDING
    gate_notes: list[str] = field(default_factory=list)
    outcome_note: Optional[str] = None
    resolved_at: Optional[datetime] = None