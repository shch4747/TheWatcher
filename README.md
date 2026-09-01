# TheWatcher — ARIES multi-agent society

A set of cooperating agents for **ARIES** (the AI/ML club of IIT Delhi) that
keep the club's knowledge, projects, and research current with little manual
effort. The agents don't call each other directly — they coordinate through a
shared **Lapis wiki**, which is both ARIES's knowledge base (members,
projects, events, research) and the message bus between agents. Each agent
reads what it needs from the wiki and writes its output back; a change to a
page is what wakes the next agent.

```
                    ┌─────────────────────────────┐
   WhatsApp  ─────▶ │        Lapis wiki           │ ◀──── arXiv / HF / HN
   (gowa)           │  (knowledge base + bus)     │       (research sources)
                    └──────┬───────┬───────┬──────┘
                           ▲       ▲       ▲
              WA agent ────┘       │       └──── Research agent
              (ingest + notify)    │             (papers → project pages)
                                   │
                            Innovation agent
                         (new ideas from everything)
```

Everything runs on cheap infrastructure: **Lapis** (self-hosted Obsidian-sync
+ REST vault, `lapis.dvenom.in`), **gowa** (WhatsApp Web multi-device REST
gateway) for the WhatsApp side, and **OpenRouter** for LLM calls.

## The three agents

| Agent | Language | Trigger | Reads | Writes | Status |
|---|---|---|---|---|---|
| **WA agent** | Python | N messages / X hours per group; and other agents poking it | WhatsApp group messages | Research digest, idea inbox, project pages, agent triggers; sends summaries back to WhatsApp | Built, tested on mocks; awaiting live gowa + Lapis token |
| **Research agent** | TypeScript | The WhatsApp digest page changed (hourly poll); + every ~2 days | `WhatsApp/ResearchDigest.md`, project pages, arXiv/HF/HN | Matched papers → each project's page / `Interesting To Research.md` / `Trending Feed.md` | Built, deployable as GitHub Actions |
| **Innovation agent** | Python | Manual / weekly | Project history, research, org snapshot | Idea pitches → human review → wiki | MVP built (mock memory) |

### WA agent (`wa-agent/`)

The club's WhatsApp presence. It watches project + announcement groups, and
on a per-group cadence it classifies what was said (via **Kimi K2.6**) into
typed signals and routes each to where the owning agent expects it: research
topics to `WhatsApp/ResearchDigest.md`, ideas to `Ideas/Inbox.md`, project
updates to the project's page. It's also the **outbound gateway** — the only
thing that talks to WhatsApp — so when another agent has something worth
sharing, the WA agent decides which group it belongs to (via a group→project
registry), summarises it in that group's context, and sends it. It also posts
event/task reminders. Preprocessing handles the messy parts of a real chat:
dedup, a cheap noise pre-filter (to save tokens), OCR for screenshots, and
"ask a human to TLDR this" for important voice notes. See `wa-agent/README.md`
for the design and `wa-agent/testing/test_readme.md` to test it.

### Research agent (`research-agent/`)

Keeps ARIES abreast of relevant work. Three jobs: (1) **WhatsApp-reactive** —
when the WA agent's research digest changes, it extracts the topics, *reframes
them through an AI/ML lens*, searches arXiv + Hugging Face, and pushes
matching papers to the project they're about; (2) **project-relevant** — every
couple of days re-checks each project's topics against fresh results; (3)
**trending** — a popularity-sorted snapshot of new AI/ML papers and HN
stories. Strong dedup (never surfaces the same paper twice) and a full audit
log. Ships with `LOCAL_MODE`/`DRY_RUN` test modes and deploys as two GitHub
Actions workflows. See `research-agent/README.md`.

### Innovation agent (`innovation-agent/`)

Proposes new things ARIES could take on. Three "lanes" generate ideas —
**grounded** (from patterns in the club's own history, e.g. a capability used
once and forgotten, or a failure cause that keeps recurring), **bridged**
(from a new research finding connected to an existing capability), and
**free** (unanchored) — all passing through one shared **gate** (redundancy →
relevance → novelty) before a human sees the survivors; outcomes are written
back to memory. Runs manually or weekly. See `innovation-agent/README.md`.

## How they fit together

The intended orchestration (from the design notes):

- A WhatsApp message flows: **chat → WA agent → Project agent**, and the WA
  agent triggers the **Project agent before the Research agent**.
- The **Research agent** is otherwise independent, running on its own schedule
  and reacting to the WhatsApp research digest changing.
- The **Innovation agent** runs manually/weekly and consumes the *outputs* of
  the others as its inputs, ensuring they've run recently first.
- All outbound WhatsApp messaging goes through the WA agent, so rate-limiting
  and phrasing live in one place.

## Shared conventions

- **Lapis is the single source of truth.** Every agent talks to it over the
  same REST API (`/api/vaults/:id/{manifest,files/*,search}`), authenticated
  with a device bearer token. The Python agents wrap this in a `LapisAdapter`
  behind a memory interface; the TS agent has its own `lapisClient.ts`. Both
  use the same env-var names (`LAPIS_BASE_URL`, `LAPIS_VAULT_ID`,
  `LAPIS_BEARER_TOKEN`), so one `.env` serves everything.
- **OpenRouter** for all LLM calls (Kimi K2.6 for the WA agent; a configurable
  model for the others).
- Notes are **markdown + YAML frontmatter**; pages are Title-Cased
  (`Projects/`, `WhatsApp/ResearchDigest.md`, …).

## Repo layout

```
TheWatcher/
├── wa-agent/          # WhatsApp agent (Python) — ingest + outbound gateway
│   ├── *.py           # package modules
│   ├── run_live.py    # the deployable receiver
│   └── testing/       # unit tests + check_lapis.py / check_gowa.py + test_readme.md
├── research-agent/    # Research agent (TypeScript/Node) — GitHub Actions
│   └── src/
└── innovation-agent/  # Innovation agent (Python) — idea generation
```

## Getting started

Each agent has its own README with setup and testing steps. To bring the
WhatsApp side live, start with `wa-agent/testing/test_readme.md` — it walks
through testing the Lapis connection and the WhatsApp login before wiring
anything together.
