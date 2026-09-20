"""Phase 6 benchmark harness (docs/Plan - Watcher v1.md): the harness
itself is tested here against fixture clients - actually picking a
Worker model needs live credentials and real conversations neither this
repo nor CI has, so there is deliberately no "winner" asserted."""
from __future__ import annotations

import json
from pathlib import Path

from shared.models.interface import BenchmarkCase, run_benchmark
from shared.models.text import TextModelClient

EVAL_SET = Path(__file__).parent / "fixtures" / "eval" / "worker_model_eval.json"


class FakeClient(TextModelClient):
    def __init__(self, model_name: str, text: str):
        self.model_name = model_name
        self.text = text

    async def generate(self, prompt: str, system: str | None = None):  # type: ignore[override]
        from shared.models.schemas import TextResult

        return TextResult(text=self.text, input_tokens=7, output_tokens=3)


def _load_cases() -> list[BenchmarkCase]:
    data = json.loads(EVAL_SET.read_text())
    return [
        BenchmarkCase(
            name=c["name"],
            prompt=c["prompt"],
            system=c.get("system"),
            expected_keywords=tuple(c["expected_keywords"]),
        )
        for c in data
    ]


async def test_eval_fixture_loads_and_has_expected_shape():
    cases = _load_cases()
    assert len(cases) == 3
    assert all(c.expected_keywords for c in cases)


async def test_run_benchmark_scores_keyword_coverage():
    cases = _load_cases()
    good_client = FakeClient("good-model", "Aira booked the seminar hall for Friday.")
    bad_client = FakeClient("bad-model", "irrelevant output with no matches")

    report = await run_benchmark({"good": good_client, "bad": bad_client}, cases[:1])

    assert report.reports["good"].keyword_coverage > report.reports["bad"].keyword_coverage
    assert report.reports["good"].total_input_tokens == 7


async def test_benchmark_report_renders_markdown_with_no_verdict():
    cases = _load_cases()
    client = FakeClient("model-a", "some output")
    report = await run_benchmark({"model-a": client}, cases)
    text = report.render()
    assert "Worker model benchmark" in text
    assert "model-a" in text
    assert "human review" in text.lower()
