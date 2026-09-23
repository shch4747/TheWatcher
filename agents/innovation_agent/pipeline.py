"""
Wires triggers -> lanes -> gate -> human queue -> memory writeback.
Two pipelines run side by side: remedial is real (via `vault`); bridged,
frontier, event are still on `memory` until each gets its turn.
"""
import uuid
from datetime import datetime, timezone

from memory_interface import MemoryInterface
from schemas import Idea, Pitch
from shared.wiki.interface import VaultClient
import interface
import lanes
import gate
import verification


def _to_pitches(ideas, memory: MemoryInterface) -> list[Pitch]:
    history = memory.get_pitch_history()
    snapshot = memory.get_org_snapshot()
    pitches = []
    for idea in ideas:
        idea = verification.verify_idea(idea, memory.get_record)
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


def _to_pitches_real(ideas: list[Idea], vault: VaultClient) -> list[Pitch]:
    reviewed = interface.get_reviewed_ideas_sync(vault)
    history = [
        {"title": r["title"], "outcome": "reject" if r["verdict"] == "down" else r["verdict"]}
        for r in reviewed
    ]
    pitches = []
    for idea in ideas:
        idea = verification.verify_idea(idea, lambda pid: interface.get_record_sync(vault, pid))
        result = gate.run_gate(idea, history, "")
        if result.passed:
            interface.publish_idea_sync(vault, idea)
        pitch = Pitch(
            id=str(uuid.uuid4())[:8],
            idea=idea,
            created_at=datetime.now(timezone.utc),
            status=result.status,
            gate_notes=result.notes,
        )
        pitches.append(pitch)
    return pitches


def handle_project_closed(vault: VaultClient, project_path: str) -> list[Pitch]:
    ideas = lanes.grounded_on_closure(vault, project_path)
    return _to_pitches_real(ideas, vault)


def handle_research_deepdive(memory: MemoryInterface, entry_id: str) -> list[Pitch]:
    return _to_pitches(lanes.bridged_on_research(memory, entry_id), memory)


def run_weekly_sweep(vault: VaultClient, memory: MemoryInterface) -> list[Pitch]:
    remedial_ideas = lanes.grounded_weekly_sweep(vault)
    frontier_ideas = lanes.free_weekly(memory)
    return _to_pitches_real(remedial_ideas, vault) + _to_pitches(frontier_ideas, memory)


def run_events_sweep(memory: MemoryInterface) -> list[Pitch]:
    return _to_pitches(lanes.events_weekly_sweep(memory), memory)


def record_human_outcome(memory: MemoryInterface, pitch_id: str, outcome: str, note: str = "") -> None:
    memory.write_outcome(pitch_id, outcome, note)