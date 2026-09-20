---
status: proposed
date: TODO
---
# Worker model choice: TODO

Fill this in after running `uv run python scripts/run_worker_benchmark.py`
(or a real, recorded conversation eval set - the shipped
`tests/fixtures/eval/worker_model_eval.json` is a small synthetic
placeholder, not the Spec's 10-20 recorded conversations) with real
`MODELS_API_KEY` credentials, and reading the outputs yourself. The
Spec's exit criterion is human review of the actual generated text, not
the keyword-coverage number the harness prints.

Candidates benchmarked: GPT-6 Luna, GLM 5.3 Flash, claude-haiku-4-5
(ADR-0006's shortlist plus the Plan's third candidate).

**Decision:** TODO - which model, and why (cost, latency, output quality
on your own read of the transcripts).

**Consequences:** TODO - update `WORKER_MODEL_NAME` in `.env` /
`shared/config.py`'s default to match.
