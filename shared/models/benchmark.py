"""Worker model benchmark harness (Plan Phase 6: "Worker model benchmark
... on the recorded eval set; pick and record in an ADR; cost report per
batch in the run ledger"). This module runs the harness - it cannot
pick a winner for you. The Spec's own exit criterion is human review
("reviewed by humans"), so `BenchmarkReport` is a cost/latency/keyword-
coverage summary to inform that review, not a verdict.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from shared.models.calls import generate
from shared.models.text import TextModelClient


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    prompt: str
    system: str | None = None
    expected_keywords: tuple[str, ...] = ()


@dataclass
class CaseResult:
    case_name: str
    output: str
    input_tokens: int
    output_tokens: int
    latency_s: float
    keywords_found: int
    keywords_total: int


@dataclass
class ModelReport:
    model_name: str
    results: list[CaseResult] = field(default_factory=list)

    @property
    def total_input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.results)

    @property
    def total_output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.results)

    @property
    def mean_latency_s(self) -> float:
        return sum(r.latency_s for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def keyword_coverage(self) -> float:
        found = sum(r.keywords_found for r in self.results)
        total = sum(r.keywords_total for r in self.results)
        return found / total if total else 0.0


@dataclass
class BenchmarkReport:
    reports: dict[str, ModelReport]

    def render(self) -> str:
        lines = ["# Worker model benchmark", ""]
        lines.append("| Model | Tokens in | Tokens out | Mean latency (s) | Keyword coverage |")
        lines.append("| --- | --- | --- | --- | --- |")
        for name, report in self.reports.items():
            lines.append(
                f"| {name} | {report.total_input_tokens} | {report.total_output_tokens} | "
                f"{report.mean_latency_s:.2f} | {report.keyword_coverage:.0%} |"
            )
        lines.append("")
        lines.append(
            "Coverage is a crude keyword-containment proxy, not a quality score - "
            "the Spec's actual exit criterion is human review of the outputs below."
        )
        for name, report in self.reports.items():
            lines.append(f"\n## {name} outputs\n")
            for r in report.results:
                lines.append(f"### {r.case_name}\n\n{r.output}\n")
        return "\n".join(lines)


async def run_case(client: TextModelClient, case: BenchmarkCase) -> CaseResult:
    start = time.monotonic()
    result = await generate(client, "worker", case.prompt, system=case.system)
    elapsed = time.monotonic() - start
    found = sum(1 for kw in case.expected_keywords if kw.lower() in result.text.lower())
    return CaseResult(
        case_name=case.name,
        output=result.text,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_s=elapsed,
        keywords_found=found,
        keywords_total=len(case.expected_keywords),
    )


async def run_benchmark(clients: dict[str, TextModelClient], cases: list[BenchmarkCase]) -> BenchmarkReport:
    reports = {}
    for model_name, client in clients.items():
        report = ModelReport(model_name=model_name)
        for case in cases:
            report.results.append(await run_case(client, case))
        reports[model_name] = report
    return BenchmarkReport(reports=reports)
