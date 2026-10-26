"""Local web UI for the testbench.   python -m testbench serve

Binds to 127.0.0.1 only: the data is real chat content.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from testbench import store
from testbench.config import ExperimentConfig
from testbench.groups import DataError
from testbench.web import view

app = FastAPI(title="testbench")
INDEX = Path(__file__).with_name("index.html")


def _exp(exp_id: str) -> Path:
    try:
        return store.exp_dir(exp_id)
    except KeyError:
        raise HTTPException(404, f"no experiment {exp_id}") from None


def _guard(fn, *a, **k):
    try:
        return fn(*a, **k)
    except (DataError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/api/experiments")
def experiments():
    out = []
    for i in store.list_ids():
        try:
            cfg = store.load_config(i)
        except Exception:  # noqa: BLE001
            continue
        st = store.status(i)
        out.append(
            {
                "id": i,
                "name": cfg.name,
                "state": st["state"],
                "created_at": cfg.created_at,
                "groups": cfg.groups,
                "date_from": cfg.date_from,
                "date_to": cfg.date_to,
                "env": cfg.env,
            }
        )
    return out[::-1]


@app.get("/api/experiments/{exp_id}")
def experiment(exp_id: str):
    d = _exp(exp_id)
    res = store.result(exp_id) or {}
    return {
        "id": exp_id,
        "config": store.load_config(exp_id).__dict__,
        "status": store.status(exp_id),
        "progress": store.progress(exp_id, 8),
        "channels": view.list_channels(d),
        "errors": res.get("errors", []),
        "settings": res.get("settings"),
        "model_usage": res.get("model_usage"),
        "cache": res.get("cache"),
        "wakes": res.get("wakes", []),
        "inbox": res.get("inbox", {}),
        "warnings": res.get("dataset_warnings", []),
    }


@app.get("/api/experiments/{exp_id}/stream")
def stream(exp_id: str, jid: str):
    return view.build_stream(_exp(exp_id), jid)


@app.get("/api/experiments/{exp_id}/page", response_class=PlainTextResponse)
def page(exp_id: str, path: str):
    vault = (_exp(exp_id) / "vault").resolve()
    target = (vault / path).resolve()
    if vault not in target.parents or target.suffix != ".md" or not target.is_file():
        raise HTTPException(404, "no such page")
    return target.read_text(encoding="utf-8", errors="replace")


@app.post("/api/experiments")
def create(body: dict = Body(...)):  # noqa: B008
    fields = {
        k: body.get(k)
        for k in (
            "name",
            "data_dir",
            "groups",
            "date_from",
            "date_to",
            "timezone",
            "env",
            "cache",
            "include_media",
            "lifecycle_days_after",
            "seed_vault",
            "notes",
        )
        if body.get(k) not in (None, "")
    }
    cfg = ExperimentConfig(name=fields.pop("name", ""), data_dir=fields.pop("data_dir", ""), **fields)
    return {"id": _guard(store.create, cfg)}


@app.post("/api/experiments/{exp_id}/run")
def run(exp_id: str, rerun: bool = False):
    _exp(exp_id)
    _guard(store.start, exp_id, rerun)
    return {"ok": True}


@app.post("/api/experiments/{exp_id}/reset")
def reset(exp_id: str):
    _exp(exp_id)
    _guard(store.reset, exp_id)
    return {"ok": True}


@app.post("/api/experiments/{exp_id}/stop")
def stop(exp_id: str):
    _exp(exp_id)
    return {"stopped": store.stop(exp_id)}


@app.post("/api/experiments/{exp_id}/clone")
def clone(exp_id: str, body: dict = Body(default={})):  # noqa: B008
    _exp(exp_id)
    return {
        "id": _guard(
            store.clone,
            exp_id,
            body.get("name"),
            env=body.get("env") or {},
            date_from=body.get("date_from"),
            date_to=body.get("date_to"),
        )
    }


@app.delete("/api/experiments/{exp_id}")
def delete(exp_id: str):
    _exp(exp_id)
    _guard(store.delete, exp_id)
    return {"ok": True}
