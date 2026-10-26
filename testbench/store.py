"""Experiment folders: create / list / start / reset / clone / delete.

    testbench_data/
      llm_cache.sqlite              shared by all experiments
      experiments/exp-001/
        config.json                 the only thing reset keeps
        watcher.db                  this experiment's gateway DB (buffer, channels, members, model calls)
        vault/                      this experiment's wiki - the "separate Lapis"
        status.json  progress.jsonl  result.json  run.log

No `shared/` imports: the runner is a *subprocess* started with this experiment's
environment, which is what isolates experiments from each other and from the real system.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from testbench.config import ExperimentConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
HOME = Path(os.environ.get("TESTBENCH_HOME") or REPO_ROOT / "testbench_data")
OUTPUTS = (
    "watcher.db",
    "watcher.db-wal",
    "watcher.db-shm",
    "vault",
    "status.json",
    "progress.jsonl",
    "result.json",
    "run.log",
)
_ID = re.compile(r"^exp-(\d{3,})$")


def experiments_dir() -> Path:
    return HOME / "experiments"


def cache_path() -> Path:
    return Path(os.environ.get("TESTBENCH_CACHE") or HOME / "llm_cache.sqlite")


def exp_dir(exp_id: str) -> Path:
    d = experiments_dir() / exp_id
    if not _ID.match(exp_id) or not d.is_dir():
        raise KeyError(f"no experiment {exp_id!r}")
    return d


def list_ids() -> list[str]:
    root = experiments_dir()
    return sorted(p.name for p in root.iterdir() if _ID.match(p.name)) if root.is_dir() else []


def _next_id() -> str:
    nums = [int(_ID.match(i).group(1)) for i in list_ids()]
    return f"exp-{(max(nums) + 1 if nums else 1):03d}"


def create(cfg: ExperimentConfig) -> str:
    cfg.validate()
    experiments_dir().mkdir(parents=True, exist_ok=True)
    while True:  # mkdir is the lock: two creators can't get the same id
        exp_id = _next_id()
        try:
            (experiments_dir() / exp_id).mkdir()
            break
        except FileExistsError:
            continue
    (experiments_dir() / exp_id / "config.json").write_text(cfg.to_json())
    return exp_id


def load_config(exp_id: str) -> ExperimentConfig:
    return ExperimentConfig.load(exp_dir(exp_id) / "config.json")


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def status(exp_id: str) -> dict:
    d = exp_dir(exp_id)
    f = d / "status.json"
    st = json.loads(f.read_text()) if f.is_file() else {"state": "new"}
    if st.get("state") == "running" and not _alive(st.get("pid")):
        st = {
            **st,
            "state": "crashed",
            "error": st.get("error") or f"runner exited unexpectedly - see {d / 'run.log'}",
        }
    return st


def result(exp_id: str) -> dict | None:
    f = exp_dir(exp_id) / "result.json"
    return json.loads(f.read_text()) if f.is_file() else None


def progress(exp_id: str, last: int = 20) -> list[dict]:
    f = exp_dir(exp_id) / "progress.jsonl"
    if not f.is_file():
        return []
    return [json.loads(line) for line in f.read_text().splitlines()[-last:] if line.strip()]


def build_env(cfg: ExperimentConfig, d: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(cfg.env)
    env.update(  # pinned last so nothing above can win
        DATABASE_URL=f"sqlite+aiosqlite:///{(d / 'watcher.db').resolve()}",
        VAULT_ROOT=str((d / "vault").resolve()),
        OTEL_ENABLED="false",
        LAPIS_TOKEN="",
        # nothing listens here: an accidental send fails instead of messaging real people
        GOWA_BASE_URL="http://127.0.0.1:9",
        GOWA_BASIC_AUTH="",
        PYDANTIC_AI_NO_BANNER="1",
        TESTBENCH_EXPERIMENT_DIR=str(d.resolve()),
        TESTBENCH_CACHE=str(cache_path().resolve()),
    )
    return env


def has_outputs(exp_id: str) -> bool:
    d = exp_dir(exp_id)
    return any((d / n).exists() for n in OUTPUTS)


def start(exp_id: str, rerun: bool = False) -> int:
    """Start the runner in the background. Returns its pid."""
    d = exp_dir(exp_id)
    cfg = load_config(exp_id)
    cfg.validate()
    if status(exp_id).get("state") in ("running", "queued"):
        raise RuntimeError(f"{exp_id} is already running")
    if has_outputs(exp_id):
        if not rerun:
            raise RuntimeError(f"{exp_id} already has results; reset it, clone it, or pass rerun=True")
        reset(exp_id)
    (d / "status.json").write_text(
        json.dumps({"state": "queued", "queued_at": datetime.now(UTC).isoformat()})
    )
    log = open(d / "run.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "testbench.run", str(d)],
        cwd=REPO_ROOT,
        env=build_env(cfg, d),
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc.pid


def reset(exp_id: str) -> None:
    """Back to a clean slate: keeps config.json, removes the DB, wiki and results."""
    if status(exp_id).get("state") in ("running", "queued"):
        raise RuntimeError(f"{exp_id} is running; stop it first")
    d = exp_dir(exp_id)
    for name in OUTPUTS:
        p = d / name
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()


def clone(exp_id: str, name: str | None = None, **overrides) -> str:
    """New experiment from an existing config (not its results)."""
    cfg = load_config(exp_id)
    cfg.name = name or f"{cfg.name} (copy)"
    cfg.created_at = datetime.now(UTC).isoformat(timespec="seconds")
    for k, v in overrides.items():
        if k == "env":
            cfg.env = {**cfg.env, **v}
        elif v is not None:
            setattr(cfg, k, v)
    return create(cfg)


def delete(exp_id: str) -> None:
    if status(exp_id).get("state") in ("running", "queued"):
        raise RuntimeError(f"{exp_id} is running; stop it first")
    shutil.rmtree(exp_dir(exp_id))


def stop(exp_id: str) -> bool:
    import signal

    st = status(exp_id)
    if st.get("state") != "running" or not _alive(st.get("pid")):
        return False
    os.killpg(os.getpgid(st["pid"]), signal.SIGTERM)
    (exp_dir(exp_id) / "status.json").write_text(
        json.dumps({**st, "state": "stopped", "finished_at": datetime.now(UTC).isoformat()})
    )
    return True
