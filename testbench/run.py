"""The experiment runner. Started as a subprocess by `store.start()`:

    python -m testbench.run <experiment dir>

with DATABASE_URL / VAULT_ROOT / settings overrides already in the environment,
so every `shared` module (engine, settings) binds to *this* experiment.

Flow: parse+merge exports -> init DB -> build vault (LocalDirClient in the
experiment folder) -> seed channels/members -> load messages -> run the real
`IngestionPipeline` per group (batch by batch) -> optional lifecycle pass ->
collect threads, inbox contents, wake-ups, model usage -> result.json.

Nothing in the agent is patched except one recording hook on the inbox wake-up
registry, which is how "agent X was pinged" gets captured (and will capture
fanout pings as soon as fanout posts triggering inbox items).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import traceback
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

MAX_DRAIN_BATCHES = 100  # same safety bound as IngestionPipeline.run_drained
SETTINGS_SNAPSHOT = (
    "assignment_mode",
    "batch_n",
    "batch_t_minutes",
    "batch_quiet_minutes",
    "message_concat_window_minutes",
    "ingest_max_prompt_tokens",
    "ingest_max_messages_per_call",
    "worker_model_name",
    "assignment_model_name",
    "ingest_model_name",
    "decision_confidence_threshold",
    "thread_stale_days",
    "thread_context_max_chars",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Recorder:
    def __init__(self, d: Path) -> None:
        self.d = d
        self.status: dict = {}

    def set_status(self, **kw) -> None:
        self.status.update(kw)
        (self.d / "status.json").write_text(json.dumps(self.status, default=str))

    def event(self, **kw) -> None:
        with open(self.d / "progress.jsonl", "a") as fh:
            fh.write(json.dumps({"t": _now(), **kw}, default=str) + "\n")


def _guard(d: Path) -> None:
    """Refuse to run against anything but this experiment's own database."""
    expected = f"{(d / 'watcher.db').resolve()}"
    if os.environ.get("TESTBENCH_EXPERIMENT_DIR") != str(d.resolve()) or not os.environ.get(
        "DATABASE_URL", ""
    ).endswith(expected):
        sys.exit("testbench.run must be started via `testbench run` (it sets the isolated environment)")


async def amain(d: Path) -> None:
    # Imported here, after the guard: these modules read the environment at import time.
    from agents.wa_agent.interface import IngestionPipeline, run_lifecycle_for_channel
    from shared.config import settings
    from shared.db import ModelCall, get_session, init_db
    from shared.gateway.interface import get_channel
    from shared.inbox import interface as inbox_mod
    from shared.inbox.interface import Inbox
    from shared.models.interface import assignment_model, openrouter_jev_client, wa_worker_model
    from shared.observability.interface import ObservedVaultClient, current, ingest_run
    from shared.wiki.interface import LocalDirClient, ThreadStore
    from sqlalchemy import func, select

    from testbench.config import ExperimentConfig
    from testbench.dataset import load_dataset
    from testbench.llm_cache import CacheStore, CachingClient
    from testbench.load import load_group_messages
    from testbench.seed import seed_group

    rec = Recorder(d)
    rec.set_status(state="running", pid=os.getpid(), started_at=_now())
    cfg = ExperimentConfig.load(d / "config.json")

    ds = load_dataset(
        Path(cfg.data_dir),
        groups=cfg.groups,
        date_from=cfg.date_from,
        date_to=cfg.date_to,
        tz=cfg.timezone,
        include_media=cfg.include_media,
    )
    rec.event(
        kind="dataset",
        groups=[g.group.title for g in ds.groups],
        messages=sum(len(g.messages) for g in ds.groups),
        warnings=ds.warnings,
    )

    await init_db()
    vault_root = d / "vault"
    if cfg.seed_vault and not vault_root.exists():
        shutil.copytree(cfg.seed_vault, vault_root)
    vault = ObservedVaultClient(LocalDirClient(vault_root))

    wakes: list[dict] = []
    registry = inbox_mod._default_consumers
    original_wake = registry.wake

    def recording_wake(agent: str) -> None:
        wakes.append({"t": _now(), "agent": agent, "channel": current().channel_jid})
        original_wake(agent)

    registry.wake = recording_wake  # type: ignore[method-assign]

    store = CacheStore(Path(os.environ["TESTBENCH_CACHE"])) if cfg.cache else None
    worker = CachingClient(wa_worker_model(), store, "worker")
    assign_client = CachingClient(assignment_model(), store, "assignment")
    decider = None
    if settings.assignment_mode == "jev":
        decider = openrouter_jev_client()  # not cached: Jev calls are per-decision billed
        rec.event(kind="warning", text="assignment_mode=jev: Jev decisions are not cached")
    pipeline = IngestionPipeline(vault, worker, assign_client, decider)

    roster_titles = {p.wiki_title for p in ds.roster.people if p.wiki_title}
    out_groups: dict[str, dict] = {}
    errors: list[dict] = []

    for gd in ds.groups:
        g = gd.group
        entry = out_groups[g.title] = {
            "channel_jid": g.jid,
            "channel_title": g.channel_title,
            "kind": g.kind,
            "messages_loaded": len(gd.messages),
            "dropped_by_date_filter": gd.dropped_by_filter,
            "dropped_media_only": gd.dropped_media_only,
            "first_message": gd.messages[0].sent_at.isoformat() if gd.messages else None,
            "last_message": gd.messages[-1].sent_at.isoformat() if gd.messages else None,
            "exports": [asdict(r) for r in gd.reports],
            "batches": [],
            "threads": [],
        }
        try:
            await seed_group(vault, gd, roster_titles)
            n = await load_group_messages(gd)
            rec.event(kind="group_start", group=g.title, messages=n)
            channel = await get_channel(g.jid)
            assert channel is not None, f"channel {g.jid} missing after seeding"
            total = None
            async with ingest_run(forced=True) as run:
                async with run.channel(channel):
                    for i in range(MAX_DRAIN_BATCHES):
                        res = await pipeline.run_forced(g.jid)
                        if res is None:
                            break
                        entry["batches"].append(asdict(res))
                        rec.event(
                            kind="batch",
                            group=g.title,
                            batch=i + 1,
                            messages=res.message_count,
                            created=len(res.threads_created),
                            updated=len(res.threads_updated),
                        )
                        if total is None:
                            total = res
                        else:
                            total.absorb(res)
                    run.record(total)
            entry["total"] = asdict(total) if total else None

            if cfg.lifecycle_days_after is not None and gd.messages:
                at = gd.messages[-1].sent_at + timedelta(days=cfg.lifecycle_days_after)
                entry["lifecycle"] = {
                    "at": at.isoformat(),
                    **await run_lifecycle_for_channel(vault, channel, at),
                }

            threads = await ThreadStore(vault, channel).threads(("active", "stale", "ended"), order="recent")
            entry["threads"] = [
                {
                    "slug": t.slug,
                    "path": t.path,
                    "title": t.title,
                    "state": t.state,
                    "summary": t.summary,
                    "participants": t.participants,
                    "message_count": len(t.message_ids),
                    "last_message_at": t.last_message_at.isoformat() if t.last_message_at else None,
                }
                for t in threads
            ]
            rec.event(kind="group_done", group=g.title, threads=len(threads))
        except Exception as exc:  # noqa: BLE001 - one group failing must not lose the others
            errors.append(
                {"group": g.title, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()}
            )
            rec.event(kind="group_error", group=g.title, error=str(exc))

    # inbox pages written by the agent(s), as structured data
    inbox: dict[str, list[dict]] = {}
    for path in await vault.list("inbox"):
        agent = Path(path).stem
        inbox[agent] = [item.model_dump(mode="json") for item in await Inbox(vault, agent).pending()]

    async with get_session() as session:
        rows = (
            await session.execute(
                select(
                    ModelCall.phase,
                    ModelCall.cost_source,
                    func.count(),
                    func.sum(ModelCall.input_tokens),
                    func.sum(ModelCall.output_tokens),
                    func.sum(ModelCall.cost_usd),
                ).group_by(ModelCall.phase, ModelCall.cost_source)
            )
        ).all()
    usage = [
        {
            "phase": r[0],
            "cost_source": r[1],
            "calls": r[2],
            "input_tokens": r[3] or 0,
            "output_tokens": r[4] or 0,
            "cost_usd": float(r[5] or 0),
        }
        for r in rows
    ]

    result = {
        "finished_at": _now(),
        "config": json.loads((d / "config.json").read_text()),
        "settings": {k: getattr(settings, k) for k in SETTINGS_SNAPSHOT},
        "dataset_warnings": ds.warnings,
        "groups": out_groups,
        "inbox": inbox,
        "wakes": wakes,
        "model_usage": usage,
        "cache": [worker.stats(), assign_client.stats()],
        "errors": errors,
    }
    (d / "result.json").write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    state = (
        "failed" if errors and len(errors) == len(ds.groups) else ("done_with_errors" if errors else "done")
    )
    rec.set_status(state=state, finished_at=_now(), errors=len(errors))


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python -m testbench.run <experiment dir>")
    d = Path(sys.argv[1])
    _guard(d)
    try:
        asyncio.run(amain(d))
    except BaseException as exc:  # noqa: BLE001
        (d / "status.json").write_text(
            json.dumps(
                {
                    "state": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "trace": traceback.format_exc(),
                    "finished_at": _now(),
                }
            )
        )
        raise


if __name__ == "__main__":
    main()
