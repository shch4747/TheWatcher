# Innovation Agent — MVP

Matches the design: three lanes (grounded / bridged / free) all query one
`MemoryInterface`, every pitch passes through one shared `gate`
(redundancy → relevance → novelty), survivors go to a human, outcomes get
written back to memory. The Project Agent's job (checking ongoing
projects for overlap) is deliberately **not** in here.

## Files

- `schemas.py` — `Idea`, `Evidence`, `Pitch`. Plain dataclasses, no ORM.
- `memory_interface.py` — the abstract `MemoryInterface`, a `MockMemory`
  with hand-written sample data (for the demo), and `LapisAdapter`, a
  real-but-guessed implementation against an assumed Obsidian-style
  vault layout (see the docstring). **This is the one file to rewrite
  once Lapis's actual file/API shape is confirmed — nothing else
  changes**, since lanes/gate/pipeline only ever call `MemoryInterface`
  methods.
- `llm_client.py` — `generate_ideas()`. Calls the LLM via a forced
  tool-call against a JSON schema mirroring `Idea`/`Evidence`, so
  responses come back structured, not as prose to re-parse. Provider is
  OpenRouter (OpenAI-compatible endpoint), pointed at `openrouter/free`
  — their auto-router that picks a free model per-request, filtered for
  tool-calling support. Falls back to a stub if `OPENROUTER_API_KEY`
  isn't set, so the demo runs with no credentials.
- `lanes.py` — `grounded_on_closure`, `grounded_weekly_sweep`,
  `bridged_on_research`, `free_weekly`. Each just builds a prompt from
  memory and calls the LLM client.
- `gate.py` — `run_gate()`: redundancy → relevance → novelty, in that
  order, with lane-aware thresholds inside each check.
- `pipeline.py` — wires triggers to lanes to gate to memory. The only
  file that touches everything else.
- `demo.py` — runs all three lanes against `MockMemory` and prints the
  result.

## Run it

```bash
pip install openai pyyaml   # openai for real generation, pyyaml optional (LapisAdapter only)
python demo.py               # runs with stubbed LLM output, no key needed
```

To hit the real model, set `OPENROUTER_API_KEY` in your shell first
(bash: `export OPENROUTER_API_KEY=...`, Windows cmd: `set OPENROUTER_API_KEY=...`),
then run `python demo.py` again.

## Known operational quirk: the free-tier router

`openrouter/free` picks a different underlying free model per call, which
means reliability varies request to request — not a bug, just the trade-off
for not being dependent on one specific model/provider staying free and
uncongested. Observed so far: some models occasionally ignore the forced
tool call and answer in prose instead (currently silently treated as "no
ideas" — see TODO); reasoning models can run out of `max_tokens` mid-answer
since they "think" before producing the structured output.

## What's genuinely unfinished (marked `TODO` in code, plus two found in testing)

- The evidence-verification loop (checking that cited `source_ids`
  actually back the claim) isn't built yet — `gate.py` only checks that
  evidence is *present*, not that it's *true*.
- Embedding-based similarity in `gate.py` (redundancy) and
  `memory_interface.py` (`get_capabilities_far_from`) — both use crude
  word-overlap/tag-exclusion placeholders right now.
- `LapisAdapter`'s frontmatter field names (`status`, `cause`,
  `technologies`, `depth`, `topic`, `added_at`) are reasonable guesses,
  not confirmed against the real vault — blocked until Lapis is set up.
- `llm_client.py`'s free lane (`free_weekly`, asks for 15 ideas per call)
  can exhaust `max_tokens=4000` on reasoning models before ever reaching
  the tool call — needs a bump, untested at what value is actually enough.
<<<<<<< HEAD
- When a model ignores the forced tool call entirely, `_call_openrouter`
  currently returns an empty list, indistinguishable from "nothing to
  pitch." A fallback that tries parsing JSON out of `message.content`
  when `tool_calls` is empty would recover cases where the model got the
  content right but used the wrong output channel.
=======
- When a model ignores the forced tool call
>>>>>>> 5c4fadd657354c240547fc9c3fdf97ea07e2654b
