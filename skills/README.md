# Skills

`SKILL.md` prompt bundles loaded by `shared.models.interface.load_skills`
from this directory and, overriding it by name, from the wiki's
`meta/skills/` (Spec: Skills).

**Ingestion does not load these.** Since
[ADR-0012](../docs/adr/0012-structured-assignment-replaces-choice.md) the
two calls that do the actual ingestion work — thread assignment, and a
thread's title/Summary/Items — use structured JSON contracts with prompts
that are constants in `agents/wa_agent/assign.py`. That path is the core
of the product and runs on every batch, so it gets a short exact prompt
rather than a loaded bundle a wiki edit can change underneath it.

`summarise-thread`, `extract-items` and `name-thread` are kept: the loader
and the wiki-override mechanism are still live infrastructure (the Chat
Agent and `scripts/run_worker_benchmark.py` use them), and these three are
the worked examples of the format.
