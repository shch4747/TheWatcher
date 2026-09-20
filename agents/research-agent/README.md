# Lapis Research Agent

The "research agent" in a multi-agent society setup: reads what the
WhatsApp-digest agent writes to Lapis, cross-references it and the society's
project pages against Hugging Face / arXiv / Hacker News, and pushes
relevant papers and articles back into the vault. One of several agents (a
WhatsApp-summarizer agent is assumed to already exist and write to
`LAPIS_WHATSAPP_PAGE_PATH`); this repo is just the research agent.

## The three jobs

| # | What | Trigger | Writes to |
|---|---|---|---|
| 1 | **WhatsApp-reactive.** Extracts what was discussed and reframes it through an AI/ML lens (a neuroscience discussion becomes "neuroscience-inspired neural network architectures", not a raw neuroscience search — see below), searches for matching papers/articles, and pushes each to the project it's about — or to a shared bucket if it isn't project-specific. | The WhatsApp digest page's content changed | The matching project's page, or `Interesting To Research.md` |
| 2 | **Project-relevant.** Independent of WhatsApp — re-checks every active/past project's own topics against fresh arXiv + HF results. | Every 2 days (floor, configurable) | Each project's own page |
| 3 | **Trending.** HF trending papers + AI/ML Hacker News stories, sorted by popularity, excluding anything already surfaced by jobs 1/2 or already discussed on WhatsApp. | Every 2 days (floor, configurable) | `Trending Feed.md` (overwritten each run — a snapshot, not a log) |

All three log every individual push — with date and time — to
`Research Agent Log.md`, so the society has a full audit trail of what the
agent added and when, not just a per-run summary.

### Job 1 only surfaces the AI/ML angle, not raw cross-domain topics

Job 1 does **not** take whatever the WhatsApp discussion covered and search
for papers on that subject directly. It first reframes each discussed topic
through an AI/ML lens — a neuroscience discussion becomes a search for
"neuroscience-inspired neural network architectures" or "computational
models of the brain", not a search for neuroscience papers in general — and
drops anything with no genuine AI/ML connection. This happens twice: once
when topics are extracted from the WhatsApp digest (`extractWhatsappTopics`
in `src/relevance.ts`), and again as a second filter when candidates are
judged for relevance (`assignCandidates`), so a bad topic extraction can't
leak an unrelated paper through.

### Never pushing the same paper twice

Two layers enforce this:
- **Within a page:** before writing, `appendUnderHeading` checks whether the
  paper's URL already appears anywhere in that page's existing content and
  skips it if so — this is a hard guarantee per-page, immune to state loss.
- **Across pages/runs:** a rolling set of every URL the agent has ever
  pushed (`recentUrls` in state, capped at `RECENT_URLS_MEMORY_SIZE`,
  default 5000) is checked *before* candidates are even sent to the model,
  so the same paper won't turn up on a project page, the common page, and
  the trending feed across different runs either. All three jobs update
  this set with exactly what they actually wrote (not what was merely
  "judged relevant" — if a page-level dedupe skipped something, it isn't
  marked as pushed).

### Why two separate GitHub Actions workflows

Job 1's trigger ("whenever the WhatsApp page updates") and jobs 2/3's
trigger ("every 2 days") are fundamentally different cadences, and Lapis has
no webhooks to push a "the page changed" event to anything. So:

- **`research-agent-whatsapp.yml`** runs hourly and is a near-no-op unless
  the WhatsApp page's content hash changed since last time — that's what
  "triggered by the page updating" collapses to without a webhook. Tighten
  the cron if you want faster reaction time; it costs almost nothing on the
  no-op runs since no LLM/API calls happen until a real change is detected.
- **`research-agent-periodic.yml`** runs daily but only actually does
  anything once the configured floor (2 days, `MIN_DAYS_BETWEEN_PERIODIC_RUNS`)
  has elapsed *for that specific job* — jobs 2 and 3 track their own last-run
  time independently, so running them together doesn't cause one to
  starve the other.

### Job 3's WhatsApp-awareness

Beyond the general dedup above, job 3 additionally excludes anything
matching topics from `recentWhatsappTopics` (kept in state), so "trending
but not yet discussed on WhatsApp" is enforced directly rather than just
hoped for.

### Sources

- **Hugging Face** — `GET /api/daily_papers?sort=trending`, used by jobs 1–3.
- **arXiv** — full-text search (`export.arxiv.org/api/query`), queried per
  extracted topic. This is deliberately *not* restricted to AI/ML keywords —
  job 1 in particular extracts whatever the WhatsApp discussion actually
  covered (e.g. neuroscience-adjacent topics work fine as arXiv queries too).
- **Hacker News** — top stories pre-filtered by an AI/ML keyword regex, used
  two ways: (a) as source material for deriving extra trending topics to
  search arXiv on, and (b) directly, as "article" candidates (title + link,
  not fetched/summarized) for job 3's news component. Stories that link
  straight to an arXiv paper are treated as papers instead of generic
  articles.

## 1. Configure

```
cd research-agent
npm install
cp .env.example .env
```

Fill in `.env` — see the comments in `.env.example` for what each var does.
The important ones to actually change from their defaults:

| Var | What |
|---|---|
| `LAPIS_BASE_URL`, `LAPIS_VAULT_ID` | Your deployed Lapis instance |
| `LAPIS_BEARER_TOKEN` | See **Auth** below |
| `LAPIS_PROJECTS_PREFIX` | Folder your project notes live in |
| `LAPIS_WHATSAPP_PAGE_PATH` | Path the WhatsApp agent writes its digest to |
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | Any `provider/model` slug from openrouter.ai/models |

### Running for free (and what happens when a free model gets pulled)

`OPENROUTER_MODEL` defaults to a free (`:free`) model so a fresh $0
OpenRouter account works immediately — no card, no credits. The catch:
OpenRouter's free-tier roster rotates, and a model that's free today can be
pulled or moved to paid-only without notice (you may see this as a 404
`"unavailable for free"` error).

To handle that without needing a code change every time it happens, the
agent automatically detects that specific error, fetches OpenRouter's live
model list, picks whichever free model is actually available right now, and
retries once. You'll see this in the logs as:

```
[openrouter] "..." isn't available for free right now (...). Looking up a currently-live free model...
[openrouter] Retrying with "...". To skip this lookup on future runs, set OPENROUTER_MODEL=... in your .env
```

It's worth updating `OPENROUTER_MODEL` to whatever it picked (as the log
suggests) so future runs skip the extra lookup call — but it's not required,
the fallback will just run again next time if needed. If *no* free models
are available at all (rare), you'll get a clear error telling you to either
add credits or try again later.

### Auth — verify this before relying on the agent unattended

Lapis's documented auth is a browser session cookie or the Obsidian plugin's
device Bearer token — there's no documented "mint a token for a script"
endpoint. For a cron job, the device Bearer token is the right one (a
session cookie will expire and break the job silently). The Lapis repo's
`examples/` folder is called out in its README as containing "agent
integrations" — **check that first**, it likely shows the intended pattern.
Failing that, run the plugin's device-code flow once by hand (Settings →
Lapis Sync → Connect) and capture the resulting token.

### Project note format assumed

No rigid frontmatter required — the model infers status/topics from
freeform content — but a `status: ongoing|completed` frontmatter field, if
you have one, makes the extraction more reliable.

## 2. Run locally

```
npm start -- --job=whatsapp   # job 1 only
npm start -- --job=projects   # job 2 only
npm start -- --job=trending   # job 3 only
npm start -- --job=periodic   # jobs 2 + 3
npm start -- --job=all        # all three (default if --job is omitted)
```

Set `FORCE_RUN=true` in `.env` to bypass the change-detection / 2-day floor
for any of these, useful when testing.

## Testing

**Start with `DRY_RUN=true`.** It runs everything for real — reads your
actual vault, calls the real HF/arXiv/HN APIs, calls the real model — but
every write to Lapis (project page edits, the common/trending pages, the
datalog, and the state file) is logged to the console instead of sent. This
is the safe way to see exactly what the agent *would* do to your vault
before letting it. Nothing about the run is a simulation except the final
write step, so what you see in dry-run output is what a real run would
produce.

```
cp .env.example .env
# fill in .env, plus:
echo "DRY_RUN=true" >> .env
echo "FORCE_RUN=true" >> .env   # skip the change/2-day checks while testing

npm install
npm start -- --job=whatsapp
```

Read the console output top to bottom — it logs each pipeline stage
(candidates found, relevance judged, per-destination push counts) followed
by `[DRY_RUN] Would write ... to "<path>"` blocks showing the exact content
each affected page would end up with. Compare that against what's actually
in your vault right now to sanity-check the diff.

A few things worth specifically testing before going live:

1. **Job 1's trigger.** Run `--job=whatsapp` with `FORCE_RUN=false` twice in
   a row without touching the WhatsApp page in between — the second run
   should log "Page unchanged since last run — skipping." with no API/model
   calls. Then edit the WhatsApp page and run again — it should do a full
   pass this time.
2. **Job 1's AI/ML framing.** Put something clearly non-AI/ML-adjacent on
   the WhatsApp page (e.g. "we're arguing about which pizza place to order
   from") alongside something with a real AI/ML angle, and confirm the
   pizza discussion gets dropped from the extracted topics while the other
   survives — check the `[job:whatsapp] AI/ML-framed topics:` log line.
3. **Dedup.** Run the same job twice with `FORCE_RUN=true` and the same
   vault state (don't run in `DRY_RUN` the second time, or the state file
   won't have actually updated). The second run should push nothing new —
   `pushed 0 item(s)` — since everything relevant already got pushed the
   first time.
4. **Jobs 2 & 3's 2-day floor.** Run `--job=periodic` once for real (not dry
   run), then again immediately with `FORCE_RUN=false` — it should log "Not
   due yet — skipping." for both. Set `MIN_DAYS_BETWEEN_PERIODIC_RUNS=0` in
   `.env` temporarily if you want to test the "due" path without waiting.
5. **The datalog itself.** After a real (non-dry-run) test push, open
   `Research Agent Log.md` in the vault and confirm each pushed item got
   its own dated/timestamped line, plus a run-complete summary line.

**When you're confident, flip `DRY_RUN=false`, keep `FORCE_RUN=true` for one
more real run to confirm actual writes land where expected, then set
`FORCE_RUN=false`** so the deployed cron jobs behave normally (respecting
the change-detection and 2-day floor instead of always running).

**Testing the GitHub Actions workflows** before trusting the cron: push the
repo with secrets/variables configured, then trigger each workflow manually
from the Actions tab (`workflow_dispatch` — both workflows support a
`force_run` input checkbox) rather than waiting for the schedule. Check the
run's logs the same way as local testing, and check the vault pages
afterward.

## Checking output on your laptop (no Lapis needed)

You don't need Lapis set up at all to see what the agent finds — set
`LOCAL_MODE=true` and every page it would read/write in Lapis becomes a
plain file under `LOCAL_OUTPUT_DIR` (default `./local-output`) on your
machine instead. This still makes real calls to HF/arXiv/HN and to
OpenRouter (so you need `OPENROUTER_API_KEY` either way), it just swaps out
the storage backend.

**Job 3 (trending) is the easiest to try this way** — it doesn't read
project pages or the WhatsApp page at all, so there's nothing to set up
beyond an OpenRouter key:

```
cp .env.example .env
# fill in OPENROUTER_API_KEY (Lapis vars can stay blank)
echo "LOCAL_MODE=true" >> .env
echo "FORCE_RUN=true" >> .env

npm install
npm start -- --job=trending
```

Then open the folder it created:

```
ls ./local-output
cat "./local-output/Trending Feed.md"
cat "./local-output/Research Agent Log.md"
```

`Trending Feed.md` has the actual paper/article list with reasons,
popularity-sorted; `Research Agent Log.md` has the timestamped push
records. Running it again immediately (still `FORCE_RUN=true`) will show
`Not due yet` unless you also bump `MIN_DAYS_BETWEEN_PERIODIC_RUNS=0`, or
you can just delete `./local-output/Research/.agent-state.json` between
runs to reset it.

**For job 1 or 2 locally**, you additionally need the input files to exist
under `local-output/` yourself, since there's no WhatsApp/project-writing
agent creating them for you here:

```
mkdir -p "./local-output/Projects"
mkdir -p "./local-output/WhatsApp"

cat > "./local-output/Projects/Test Project.md" << 'EOF'
# Test Project
status: ongoing

Building a small transformer from scratch to understand attention better.
EOF

cat > "./local-output/WhatsApp/ResearchDigest.md" << 'EOF'
# WhatsApp Digest

Members discussed sparse attention mechanisms and whether flash attention
tricks apply to their from-scratch transformer project.
EOF

npm start -- --job=whatsapp   # or --job=projects
```

Then check `./local-output/Projects/Test Project.md` — it'll have gained a
"🔎 Suggested Research (auto)" section if anything relevant was found.

`LOCAL_MODE` and `DRY_RUN` can combine too: `LOCAL_MODE=true DRY_RUN=true`
does everything above except the final file write, printing what it would
have written to the console instead — useful if you want to iterate on
prompts without even local files changing between runs.

## 3. Deploy as GitHub Actions

1. Push this `research-agent/` folder (with a committed `package-lock.json`
   — run `npm install` once and commit the lockfile) into your society's repo.
2. Settings → Secrets and variables → Actions → add **secrets**:
   `LAPIS_BASE_URL`, `LAPIS_VAULT_ID`, `LAPIS_BEARER_TOKEN`,
   `OPENROUTER_API_KEY`, `HF_TOKEN` (optional).
3. Add **variables** (optional — defaults are used otherwise):
   `LAPIS_PROJECTS_PREFIX`, `LAPIS_WHATSAPP_PAGE_PATH`,
   `LAPIS_COMMON_RESEARCH_PAGE_PATH`, `LAPIS_TRENDING_PAGE_PATH`,
   `LAPIS_AGENT_LOG_PATH`, `LAPIS_STATE_PATH`, `OPENROUTER_MODEL`,
   `MIN_DAYS_BETWEEN_PERIODIC_RUNS`.
4. Both workflows in `.github/workflows/` are ready to go as-is. Trigger
   either manually from the Actions tab (with a force-run option) to test
   before waiting for the cron.

## Notes / things you'll likely want to tune

- **Which projects count as "active":** both jobs 1 and 2 currently match
  candidates against *all* project pages (ongoing and completed) — related-
  work suggestions can be useful even for wrapped-up projects. If you'd
  rather only match ongoing ones, filter `projects` by `.status === "ongoing"`
  in `loadProjectContexts()` (`src/projectContext.ts`) or right after calling
  it in each job.
- **arXiv rate limiting:** the client sleeps 3s between requests per arXiv's
  usage guidance, so a run with several projects/topics can take a few
  minutes — expected and fine for a cron job.
- **Cost:** each real job-1 run makes ~3 model calls (topic extraction +
  assignment), job 2 makes `1 + (number of projects)`, job 3 makes ~2. With
  a club-sized vault this should be pennies per run on most OpenRouter
  models — pick a cheaper model via `OPENROUTER_MODEL` if you want to run
  job 1's hourly poll more aggressively.
- **HN filtering:** the keyword pre-filter in `src/sources/hackernews.ts`
  (`AI_ML_HINTS`) is deliberately blunt to avoid burning model calls on the
  whole HN front page — tune it if it's too loose/strict for your society.
- **Trending feed is a snapshot, not a log:** `Trending Feed.md` gets fully
  overwritten each run (capped at `TRENDING_FEED_SIZE` items, sorted by
  popularity). If you'd rather keep history, change `writeTrendingFeed` in
  `src/pages.ts` to append a dated section instead of overwriting.
