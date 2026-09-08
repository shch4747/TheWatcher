"""
Wires triggers -> lanes -> gate -> human queue -> memory writeback.
This is the only file that knows about all the other pieces at once;
everything else stays narrow on purpose.
"""
import uuid
from datetime import datetime, timezone

from memory_interface import MemoryInterface
from schemas import Pitch
import lanes
import gate
import verification


def _to_pitches(ideas, memory: MemoryInterface) -> list[Pitch]:
    history = memory.get_pitch_history()
    snapshot = memory.get_org_snapshot()
    pitches = []
    for idea in ideas:
        idea = verification.verify_idea(idea, memory)
        result = gate.run_gate(idea, history, snapshot)
        pitch = Pitch(
            id=str(uuid.uuid4())[:8],
            idea=idea,
            created_at=datetime.now(timezone.utc),
            status=result.status,
            gate_notes=result.notes,
        )
        memory.write_pitch(pitch)
        pitches.append(pitch)
    return pitches


def handle_project_closed(memory: MemoryInterface, project_id: str) -> list[Pitch]:
    """Call this when the Project Agent (or ingestion layer) marks a
    project closed."""
    return _to_pitches(lanes.grounded_on_closure(memory, project_id), memory)


def handle_research_deepdive(memory: MemoryInterface, entry_id: str) -> list[Pitch]:
    """Call this when the Research Agent finishes a deep-dive."""
    return _to_pitches(lanes.bridged_on_research(memory, entry_id), memory)


def run_weekly_sweep(memory: MemoryInterface) -> list[Pitch]:
    """Call this on a weekly schedule (cron, GitHub Action, whatever)."""
    ideas = lanes.grounded_weekly_sweep(memory) + lanes.free_weekly(memory)
    return _to_pitches(ideas, memory)


def run_events_sweep(memory: MemoryInterface) -> list[Pitch]:
    """Call this on the same weekly schedule as run_weekly_sweep. Kept
    as its own entry point rather than folded into run_weekly_sweep, so
    the existing three lanes' call sites don't change."""
    return _to_pitches(lanes.events_weekly_sweep(memory), memory)


def record_human_outcome(memory: MemoryInterface, pitch_id: str, outcome: str, note: str = "") -> None:
    """Call this when a human resolves a pitch (pursue / defer / reject)."""
    memory.write_outcome(pitch_id, outcome, note)