---
type: note
title: Research — agent harness and decision models
tags:
  - watcher
  - research
---
# Research: harness layer and decision models (2026-09-19)

## Jev (TypeSafe AI) — the Decision Model
- "System One" model: returns typed decisions with calibrated probabilities, not text. Three question types: **Choice** (option from a list, probability per option), **Noul** (boolean with probability), **Score** (rubric → scalar).
- Latency ~70–500 ms; multiple questions per call in parallel; TypeSafe quotes ~$0.0004/decision, 20–200× faster and 40–400× cheaper than small frontier LLMs.
- Python: `langchain-typesafe` → `TypeSafeClassifier().invoke(state, questions)`; needs `TYPESAFE_API_KEY`.
- Only for bounded answer spaces. No generation.
- Sources: [LangChain guide](https://www.langchain.com/blog/building-a-harness-with-jev), [Latent Space](https://www.latent.space/p/ainews-jev-a-system-one-model-that), [Arize — as LLM judge](https://arize.com/blog/typesafe-jev-llm-judge/), [Patrick McGuinness](https://patmcguinness.substack.com/p/jev-makes-fast-and-cheap-decisions).

## Flue / PyFlue — harness candidates
- **Flue**: TypeScript agent-harness framework (headless agents, Markdown skill files, sandbox, sessions). Not usable directly after the Python pivot.
- **PyFlue**: Python port. Persistent addressable agents + bounded workflows, Markdown skills in `.agents/skills/*.md`, policy-gated sandbox (writes/shell off by default), stateful sessions, Pydantic-validated outputs with JSON repair, SSE streaming, OpenTelemetry. Pluggable backends: **Pydantic AI** (default) or **DeepAgents** (`pip install 'pyflue[deepagents]'`, LangGraph underneath).
- Maturity: ~106 stars; README: "under active development, the API may change"; in-process dispatch can lose accepted work if the process exits mid-delivery.
- Sources: [GitHub](https://github.com/SuperagenticAI/pyflue), [intro post](https://super-agentic.ai/resources/super-posts/introducing-pyflue-the-python-native-agent-harness-framework-inspired-by-flue), [Flue vs LangChain](https://tinyagents.dev/vs/flue-vs-langchain).

## Assessment
- Jev fits the Decision Model tier exactly (see ADR-0006).
- A harness buys us: skills-as-markdown, sandboxed tools, sessions, typed outputs. Pydantic AI 2.x's capabilities + tool approval cover these natively; PyFlue is not needed.

## Pydantic AI vs LangGraph + DeepAgents (decided 2026-09-19 → ADR-0005, ADR-0007)
- **Pydantic AI 2.x** (V2 June 2026): `Agent[Deps, Output]`, typed DI into tools, validated outputs with retry; V2 "capabilities" bundle tools + hooks + instructions; built-in tool approval; durable execution delegated to external engines (Temporal, DBOS, Prefect, Restate, Kitaru, …). No built-in checkpointer.
- **LangGraph**: mature checkpointers, `interrupt()` pause/resume, time-travel. **DeepAgents** 0.7.x: planning, sub-agents, filesystem memory; fast-moving, open stability issues (shutdown hang reported in 0.7.15). Weaker typing (`TypedDict` state, `Any` tools).
- Walked every agent workflow (Project: inbox consumption, nudges, health analysis, clarifying questions, `/setup`; Research: fetch → filter → digest; Innovation: dependency check → generate). Every *wait* is on a human over WhatsApp, hours–days, with tiny in-flight state and authoritative state already in wiki/kanban/DB → rows + events, not suspended graphs. No workflow has more than 3–4 steps.
- **DBOS**: in-process Python library (`pip install dbos`), no separate runtime; `DBOSAgent` wraps `Agent.run` as a workflow with model/MCP calls as checkpointed steps; SQLite or Postgres. Caveat: pending workflows are pinned to the app version that started them; workflows must be defined before `DBOS.launch()`. Kept as the escape hatch for expensive non-waiting runs.
- Sources: [Pydantic AI overview](https://pydantic.dev/docs/ai/overview/), [durable execution](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/), [releases](https://github.com/pydantic/pydantic-ai/releases), [DBOS + Pydantic AI](https://docs.dbos.dev/integrations/pydantic-ai), [LangChain changelog](https://docs.langchain.com/oss/python/releases/changelog), [DeepAgents review](https://wavect.io/blog/langchain-deep-agents-review/), [deepagents #6397](https://github.com/langchain-ai/deepagents/issues/6397).
