"""
Runnable demo: fires all four lanes against MockMemory, prints what
would land in a human's review queue, and — new — publishes every
pitch that survives the gate as a pending review stub in the local
Lapis stand-in (./lapis_vault/), so the feedback loop runs on
whatever ideas actually came out of this run instead of hardcoded
samples.

Run:  python demo.py
With a real LLM: export OPENROUTER_API_KEY=... first 
Without a key, everything still runs end to end on stub ideas.
"""
from memory_interface import MockMemory
from schemas import PitchStatus
from local_lapis_client import LocalFileLapisClient
from lapis_feedback_store import publish_pending_review
import pipeline


def _report(label: str, pitches, lapis_client) -> None:
    print(f"\n=== {label} ===")
    for p in pitches:
        print(f"[{p.status.value}] {p.idea.title} :: {p.idea.statement}")
        if p.gate_notes:
            print(f"    gate notes: {p.gate_notes}")

        if p.status == PitchStatus.PENDING:
            publish_pending_review(
                lapis_client,
                idea_id=p.id,
                lane=p.idea.origin.value,
                idea_summary=f"{p.idea.title} — {p.idea.statement}",
            )
            print(f"    -> queued for human review: lapis_vault/Ideas/Feedback/{p.idea.origin.value}.md")


def main():
    memory = MockMemory()
    lapis_client = LocalFileLapisClient("./lapis_vault")

    _report(
        "Project closure (proj-auth-b) -> grounded lane",
        pipeline.handle_project_closed(memory, "proj-auth-b"),
        lapis_client,
    )

    _report(
        "New research deep-dive (res-1) -> bridged lane",
        pipeline.handle_research_deepdive(memory, "res-1"),
        lapis_client,
    )

    _report(
        "Weekly sweep -> grounded (idle capability) + free",
        pipeline.run_weekly_sweep(memory),
        lapis_client,
    )

    _report(
        "Events sweep -> events lane",
        pipeline.run_events_sweep(memory),
        lapis_client,
    )


if __name__ == "__main__":
    main()
