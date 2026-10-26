"""testbench CLI.   python -m testbench <command> ...

check  <data_dir>              parse + merge the exports, report problems (no model calls, no cost)
new    --name N --data DIR ... create an experiment
run    <id> [--wait]           run it (in the background unless --wait)
status <id>                    state + latest progress
show   <id>                    summary of the result
list                           all experiments
reset  <id>                    wipe DB + wiki + results, keep the config
clone  <id> [--name N] [...]   same config, new experiment (override with the same flags as `new`)
stop | delete <id>
serve [--port 8765]            web UI (threads view, compare, run/reset/clone)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from testbench import store
from testbench.config import ExperimentConfig
from testbench.dataset import load_dataset
from testbench.groups import DataError


def _add_experiment_flags(p: argparse.ArgumentParser, defaults: bool) -> None:
    d = (lambda v: v) if defaults else (lambda v: None)
    p.add_argument("--groups", help="comma-separated group titles (default: all enabled)")
    p.add_argument("--from", dest="date_from", help="first day, YYYY-MM-DD (inclusive)")
    p.add_argument("--to", dest="date_to", help="last day, YYYY-MM-DD (inclusive)")
    p.add_argument(
        "--tz",
        dest="timezone",
        default=d("Asia/Kolkata"),
        help="timezone of the exports (default Asia/Kolkata)",
    )
    p.add_argument(
        "--set",
        dest="env",
        action="append",
        default=[],
        metavar="SETTING=VALUE",
        help="override a Watcher setting, e.g. --set ASSIGNMENT_MODE=jev --set BATCH_N=20 (repeatable)",
    )
    p.add_argument("--no-cache", action="store_true", help="always call the model")
    p.add_argument("--include-media", action="store_true", help="feed media-only messages to the agent")
    p.add_argument(
        "--lifecycle-days",
        type=float,
        dest="lifecycle_days_after",
        help="after replay, run the stale/archive pass at last message + N days",
    )
    p.add_argument("--seed-vault", help="folder copied in as the starting wiki")
    p.add_argument("--notes", default=d(""))


def _env(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items:
        if "=" not in it:
            raise DataError(f"--set expects SETTING=VALUE, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip().upper()] = v.strip()
    return out


def cmd_check(a) -> int:
    ds = load_dataset(
        Path(a.data_dir),
        groups=a.groups.split(",") if a.groups else None,
        date_from=a.date_from,
        date_to=a.date_to,
        tz=a.timezone,
        include_media=a.include_media,
    )
    for gd in ds.groups:
        g = gd.group
        print(f"\n== {g.title}  [{g.kind}]  -> wiki channel '{g.channel_title}'")
        for r in gd.reports:
            print(
                f"   {r.source}: {r.messages} msgs, {r.system_dropped} system, {r.deleted_dropped} deleted, "
                f"{r.media_only} media-only | dates {r.date_order} ({r.date_order_basis})"
                + (f" | {r.first:%Y-%m-%d} .. {r.last:%Y-%m-%d}" if r.first else "")
            )
        if gd.messages:
            print(
                f"   merged: {len(gd.messages)} messages, {gd.messages[0].sent_at:%Y-%m-%d %H:%M} .. "
                f"{gd.messages[-1].sent_at:%Y-%m-%d %H:%M} UTC  "
                f"(filtered out: {gd.dropped_by_filter} by date, {gd.dropped_media_only} media-only)"
            )
        else:
            print("   merged: 0 messages")
        if gd.unknown_senders:
            print(
                "   senders not in members.csv (linked members are shown by their wiki title; "
                "these appear by name):"
            )
            for person, n in sorted(gd.unknown_senders.items(), key=lambda kv: -kv[1]):
                near = ds.roster.near(person)
                hint = f"   <- maybe {', '.join(near)}? add as an alias" if near else ""
                print(f"     {person.display or '(phone number)'}: {n} msgs{hint}")
    if ds.warnings:
        print("\nWarnings:")
        for w in ds.warnings:
            print(f"  - {w}")
    return 0


def cmd_new(a) -> int:
    cfg = ExperimentConfig(
        name=a.name,
        data_dir=str(Path(a.data).resolve()),
        groups=a.groups.split(",") if a.groups else None,
        date_from=a.date_from,
        date_to=a.date_to,
        timezone=a.timezone,
        env=_env(a.env),
        cache=not a.no_cache,
        include_media=a.include_media,
        lifecycle_days_after=a.lifecycle_days_after,
        seed_vault=str(Path(a.seed_vault).resolve()) if a.seed_vault else None,
        notes=a.notes,
    )
    print(store.create(cfg))
    return 0


def _wait(exp_id: str) -> int:
    seen = 0
    while True:
        st = store.status(exp_id)
        events = store.progress(exp_id, last=10_000)
        for e in events[seen:]:
            print(
                f"  {e['t'][11:19]} {e['kind']}: "
                + ", ".join(f"{k}={v}" for k, v in e.items() if k not in ("t", "kind"))
            )
        seen = len(events)
        if st["state"] not in ("running", "queued"):
            print(f"{exp_id}: {st['state']}" + (f" - {st.get('error')}" if st.get("error") else ""))
            return 0 if st["state"] in ("done", "done_with_errors") else 1
        time.sleep(1.5)


def cmd_run(a) -> int:
    store.start(a.id, rerun=a.rerun)
    print(f"started {a.id}")
    return _wait(a.id) if a.wait else 0


def cmd_status(a) -> int:
    st = store.status(a.id)
    print(st["state"] + (f" - {st['error']}" if st.get("error") else ""))
    for e in store.progress(a.id, 8):
        print(
            f"  {e['t'][11:19]} {e['kind']}: "
            + ", ".join(f"{k}={v}" for k, v in e.items() if k not in ("t", "kind"))
        )
    return 0


def cmd_show(a) -> int:
    r = store.result(a.id)
    if r is None:
        print("no result yet")
        return 1
    print(f"{a.id}  {r['config']['name']}")
    for title, g in r["groups"].items():
        print(
            f"\n{title}: {g['messages_loaded']} messages -> {len(g['threads'])} threads "
            f"in {len(g['batches'])} batches"
        )
        for t in g["threads"]:
            print(f"  [{t['state']}] {t['title']}  ({t['message_count']} msgs)")
    for agent, items in r["inbox"].items():
        print(f"\ninbox/{agent}: {len(items)} pending")
    if r["wakes"]:
        print(f"\nwake-ups: {len(r['wakes'])} ({', '.join(sorted({w['agent'] for w in r['wakes']}))})")
    cost = sum(u["cost_usd"] for u in r["model_usage"])
    calls = sum(u["calls"] for u in r["model_usage"])
    hits = sum(c["hits"] for c in r["cache"])
    print(f"\nmodel calls: {calls} (cache hits: {hits}), cost ${cost:.4f}")
    for e in r["errors"]:
        print(f"ERROR in {e['group']}: {e['error']}")
    return 0


def cmd_list(_a) -> int:
    for i in store.list_ids():
        try:
            name = store.load_config(i).name
        except Exception:  # noqa: BLE001
            name = "?"
        print(f"{i}  {store.status(i)['state']:<17} {name}")
    return 0


def cmd_clone(a) -> int:
    over = dict(
        groups=a.groups.split(",") if a.groups else None,
        date_from=a.date_from,
        date_to=a.date_to,
        timezone=a.timezone,
        env=_env(a.env),
        cache=False if a.no_cache else None,
        include_media=True if a.include_media else None,
        lifecycle_days_after=a.lifecycle_days_after,
        seed_vault=a.seed_vault,
        notes=a.notes,
    )
    print(store.clone(a.id, a.name, **over))
    return 0


def cmd_serve(a) -> int:
    import uvicorn

    print(f"testbench UI on http://127.0.0.1:{a.port}  (local only - contains real chat content)")
    uvicorn.run("testbench.web.server:app", host="127.0.0.1", port=a.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="testbench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check")
    p.add_argument("data_dir")
    _add_experiment_flags(p, True)
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("new")
    p.add_argument("--name", required=True)
    p.add_argument("--data", required=True, help="folder with groups.csv, exports.csv, members.csv")
    _add_experiment_flags(p, True)
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("run")
    p.add_argument("id")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--rerun", action="store_true", help="reset a finished experiment first")
    p.set_defaults(fn=cmd_run)

    for name, fn in (
        ("status", cmd_status),
        ("show", cmd_show),
        ("reset", lambda a: store.reset(a.id) or 0),
        ("stop", lambda a: print("stopped" if store.stop(a.id) else "not running") or 0),
        ("delete", lambda a: store.delete(a.id) or 0),
    ):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.set_defaults(fn=fn)

    p = sub.add_parser("clone")
    p.add_argument("id")
    p.add_argument("--name")
    _add_experiment_flags(p, False)
    p.set_defaults(fn=cmd_clone)

    sub.add_parser("list").set_defaults(fn=cmd_list)
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(fn=cmd_serve)

    a = ap.parse_args(argv)
    try:
        return a.fn(a) or 0
    except (DataError, KeyError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
