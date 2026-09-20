---
status: accepted
date: 2026-09-19
supersedes: earlier draft of this ADR that named LangChain/LangGraph
---
# Watcher is a single Python application built on Pydantic AI

We are pivoting away from TypeScript. v1 is one Python app in one Docker container (Gateway, WhatsApp Agent, Scheduler, and the other agents as packages), running on a Mac mini or VPS next to gowa. Module boundaries are typed async interfaces (Pydantic in/out) declared per module; a thin HTTP/MCP adapter exposes the same contracts to out-of-process callers, so splitting into containers later is mechanical.

The agent framework is **Pydantic AI 2.x**, chosen over LangGraph + DeepAgents. The deciding factor: our architecture rests on typed interfaces between modules, and Pydantic AI makes the agent boundary the *same* type system (`Agent[Deps, Output]`, typed dependency injection, validated outputs) instead of a second, looser one (`TypedDict` state, `Any`-typed tools). Its V2 capabilities + tool-approval cover what we wanted from a harness layer, so no PyFlue on top. LangGraph's genuine advantage — durable pause/resume of a graph — turned out not to be needed (see ADR-0007). DeepAgents was additionally rejected for being 0.7.x with open stability issues and for filesystem memory that overlaps with Lapis.

**Consequences**: one language for the whole team; the earlier TS Research Agent is rewritten. We own a ~50-line Jev wrapper (Jev ships `langchain-typesafe`, not a Pydantic AI integration). No `pydantic-graph` for orchestration. If step-level checkpointing of an expensive run ever becomes necessary, **DBOS** is the escape hatch: it wraps a Pydantic AI `Agent.run` as a workflow in-process, on SQLite, with no extra runtime.
