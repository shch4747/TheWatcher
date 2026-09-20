#!/usr/bin/env python3
"""Run the Phase 6 worker-model benchmark (docs/Plan - Watcher v1.md).

Requires MODELS_API_KEY (or MODELS_BASE_URL pointed at a compatible
provider) in the environment - this makes real, billed API calls. The
eval set here is a small synthetic placeholder
(tests/fixtures/eval/worker_model_eval.json); the Spec's actual exit
criterion is a 10-20 recorded-conversation eval set reviewed by humans,
which needs real ingested conversations this repo doesn't have yet.

Usage:
    uv run python scripts/run_worker_benchmark.py [eval_set.json]

This script picks nothing for you - read the printed report (and the
per-model outputs) yourself and record the choice in a new ADR, per the
Plan's "pick and record in an ADR" instruction.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from shared.models.interface import BenchmarkCase, TextModelClient, client_for_model, run_benchmark

# ADR-0006's shortlist plus the Mentor candidate mentioned in the Plan.
# These are placeholder OpenRouter-style model ids - adjust to whatever
# your provider actually calls them before running for real.
CANDIDATE_MODELS = {
    "gpt-6-luna": "openai/gpt-6-luna",
    "glm-5.3-flash": "z-ai/glm-5.3-flash",
    "claude-haiku-4-5": "anthropic/claude-haiku-4-5",
}


def load_cases(path: Path) -> list[BenchmarkCase]:
    data = json.loads(path.read_text())
    return [
        BenchmarkCase(
            name=c["name"],
            prompt=c["prompt"],
            system=c.get("system"),
            expected_keywords=tuple(c.get("expected_keywords", [])),
        )
        for c in data
    ]


async def main() -> None:
    eval_path = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "eval" / "worker_model_eval.json"
    )
    cases = load_cases(eval_path)

    clients: dict[str, TextModelClient] = {
        label: client_for_model(model_name) for label, model_name in CANDIDATE_MODELS.items()
    }

    report = await run_benchmark(clients, cases)
    print(report.render())


if __name__ == "__main__":
    asyncio.run(main())
