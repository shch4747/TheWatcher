"""
Runnable demo: fires all four lanes, prints what would land in a human's
review queue. Remedial, bridged, and frontier run against a real local
vault (./demo_vault, a LocalDirClient); event is still on MockMemory
until its turn to go real.

Run:  python demo.py
With a real LLM: export OPENROUTER_API_KEY=... first
Without a key, everything still runs end to end on stub ideas.
"""
from pathlib import Path

from memory_interface import MockMemory
from schemas import PitchStatus
import pipeline
from shared.wiki.lapis_client import LocalDirClient


def _report(label: str, pitches) -> None:
    print(f"\n=== {label} ===")
    for p in pitches:
        print(f"[{p.status.value}] {p.idea.title} :: {p.idea.statement}")
        if p.gate_notes:
            print(f"    gate notes: {p.gate_notes}")
        if p.status == PitchStatus.PENDING:
            print("    -> published to the vault for human review (innovation/)")


def main():
    vault = LocalDirClient(root=Path("./demo_vault"))
    memory = MockMemory()

    _report(
        "Project closure (proj-auth-b) -> remedial lane",
        pipeline.handle_project_closed(vault, "projects/proj-auth-b.md"),
    )

    _report(
        "New 'deep' research finding -> bridged lane",
        pipeline.handle_research_deepdive(vault, "res-1"),
    )

    _report(
        "Weekly sweep -> remedial (idle capability) + frontier",
        pipeline.run_weekly_sweep(vault),
    )

    _report(
        "Events sweep -> event lane (still MockMemory)",
        pipeline.run_events_sweep(memory),
    )


if __name__ == "__main__":
    main()
