# testbench

Replay exported WhatsApp chats through the real WA agent against a **fake, resettable Lapis**
(one isolated wiki + database per experiment) and inspect what it produced.

Put this folder at the **repo root** (next to `agents/` and `shared/`) and add `testbench_data/` to `.gitignore`
(it will hold real chat content).

```
python -m testbench check  my_data/                 # validate exports, no model calls, free
python -m testbench new    --name baseline --data my_data/ --from 2026-09-01 --to 2026-09-14
python -m testbench run    exp-001 --wait
python -m testbench show   exp-001
python -m testbench clone  exp-001 --name "jev" --set ASSIGNMENT_MODE=jev    # same data, one change
python -m testbench reset  exp-001                  # wipe wiki+DB+results, keep config
```
Needs `MODELS_API_KEY` in your environment or `.env` (as for the agent). Never put keys in an experiment config.

## Web UI (eyeballing threads)

```
python -m testbench serve          # http://127.0.0.1:8765  (local only: it shows real chat content)
```
Create / run / reset / clone / delete experiments, then read the replayed chat with a coloured **rail** down the
left of every message: consecutive messages in one thread share a continuous bar, unassigned messages get a dashed
grey one. Click a thread label or card to dim everything else; "Open page" shows the thread's markdown. Pick
"Compare with..." to put a second experiment's rail next to the first (same messages, two groupings).
It reads `watcher.db` + `vault/` directly, so it works while an experiment is still running.

## Data folder (nothing hardcoded)

| file | one row per | columns |
|---|---|---|
| `groups.csv` | WhatsApp group | `title`, `kind` (project/event/coordis/exes/research/all/other), `initiative`, `lead`, `brief`, `enabled` |
| `exports.csv` | exported `.txt` file | `file`, `group`, `exporter`, `timezone`, `dayfirst` (dmy/mdy/blank=auto) |
| `members.csv` (optional) | person | `name`, `wiki_title` (set = linked member), `wa_identity`, `aliases` (`;`-separated) |

See `sample_data/`. Several exports of the same group are merged and de-duplicated; give each its `exporter`
so that "You" becomes the right person. `check` lists senders missing from `members.csv` with near-match hints.
Android and iPhone formats are auto-detected per line; day/month order is auto-detected per file.

## How isolation works

`run` starts a **subprocess** whose environment pins `DATABASE_URL` and `VAULT_ROOT` to
`testbench_data/experiments/<id>/`. The agent's engine/settings bind to that at import time, so experiments can't
see each other (or production). The wiki is a `LocalDirClient` over `<id>/vault/` - plain markdown you can open.
The runner also forces `GOWA_BASE_URL` to a dead address, blanks `LAPIS_TOKEN`, and refuses config overrides of
`DATABASE_URL`/`VAULT_ROOT`/`LAPIS_*`/`GOWA_*`/`OTEL_*`/anything secret-looking. No `logs` channel is ever created.

## Outputs per experiment (`testbench_data/experiments/<id>/`)

`config.json` (kept on reset) | `vault/` (the wiki: threads, `inbox/<agent>.md`, channel pages) | `watcher.db` |
`result.json` (batches, threads, inbox items, wake-ups, model usage, cache stats, errors) | `progress.jsonl` | `run.log`

## LLM cache

`testbench_data/llm_cache.sqlite`, shared by all experiments. Identical (model, system, prompt, schema) => stored
answer, `cost_source="cached"`, cost 0. Re-runs are free and deterministic; only what you change calls the model.
`--no-cache` disables it. Jev decisions (`ASSIGNMENT_MODE=jev`) are not cached.

## Known limits (by design of the data)

* `.txt` exports have no reply links, so `replied_to_id` is empty: reply routing is not exercised.
* Media is not exported; media-only messages are dropped unless `--include-media`.
* Senders without a `members.csv` row appear to the agent by display name (unlinked); phone-only senders get no name.
* Timestamps carry no timezone: default `Asia/Kolkata`, override per export in `exports.csv`.

## Tests

`python -m pytest testbench/tests` - pure Python, no Watcher code needed (parser, merge, CSVs, cache, store, web API; needs fastapi+httpx).
The runner/seed/load modules are the only parts that need the real repo.
