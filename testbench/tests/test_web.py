import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from testbench import store
from testbench.config import ExperimentConfig
from testbench.web import view
from testbench.web.server import app

SAMPLE = __import__("pathlib").Path(__file__).resolve().parents[1] / "sample_data"

THREAD = """---
type: thread
slug: {slug}
channel: "[[Autonomous Drone]]"
title: "{title}"
state: active
opened_at: 2025-10-13T04:45:00+00:00
last_message_at: 2025-10-14T13:00:00+00:00
participants: ["[[Aman Sharma]]", "Riya"]
{ids}
---
# {title}
## Summary
<!-- fence -->
{summary}
## Timeline
{timeline}
"""


def make_db(path, msgs):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE channels (id INTEGER PRIMARY KEY, jid TEXT, kind TEXT, "
        "title TEXT, initiative TEXT, cursor TEXT)"
    )
    con.execute(
        "CREATE TABLE messages_buffer (id INTEGER PRIMARY KEY, message_id TEXT, channel TEXT, "
        "event_type TEXT, payload TEXT, received_at TEXT, processed INT)"
    )
    con.execute(
        "INSERT INTO channels(jid,kind,title) VALUES "
        "('autonomous-drone@g.us','project','Autonomous Drone'),('x@g.us','unset','Hidden')"
    )
    for mid, who, text, extra in msgs:
        payload = {
            "event": "message.backfill",
            "payload": {
                "id": mid,
                "from_name": who,
                "body": text,
                "timestamp": "2025-10-13T04:45:00Z",
                **extra,
            },
        }
        con.execute(
            "INSERT INTO messages_buffer(message_id,channel,event_type,payload,processed) VALUES (?,?,?,?,1)",
            (mid, "autonomous-drone@g.us", "message.backfill", json.dumps(payload)),
        )
    con.commit()
    con.close()


@pytest.fixture()
def exp(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOME", tmp_path)
    eid = store.create(ExperimentConfig(name="demo", data_dir=str(SAMPLE)))
    d = store.exp_dir(eid)
    make_db(
        d / "watcher.db",
        [
            ("m1", "Aman", "board arrived", {}),
            ("m2", "Riya", "ok", {}),
            ("m3", "Riya", "", {"image": True}),
            ("m4", "Karan", "datasheet shared", {}),
            ("m5", "Aman", "lol", {}),
        ],
    )
    t = d / "vault" / "channels" / "autonomous-drone"
    t.mkdir(parents=True)
    (t / "20251013-flight-controller.md").write_text(
        THREAD.format(
            slug="20251013-flight-controller",
            title="Flight controller",
            summary="Board arrived; PID tuning planned.",
            ids='message_ids: ["m1", "m2"]',
            timeline="- 10:15 Aman [src:: m1]\n- 10:16 Riya [src:: m2]",
        )
    )
    # block-style list + only [src::] tags in the body for m4
    (t / "20251015-esc-review.md").write_text(
        THREAD.format(
            slug="20251015-esc-review",
            title="ESC review",
            summary="Datasheet review.",
            ids='message_ids:\n  - "m4"',
            timeline="- Karan [src:: m4]",
        )
    )
    (d / "vault" / "channels" / "autonomous-drone.md").write_text(
        "---\ntype: channel\ntitle: Autonomous Drone\n---\n# Autonomous Drone\n"
    )
    return eid


def test_frontmatter_inline_block_and_quotes():
    meta, body = view.parse_frontmatter(
        '---\ntitle: "A: b"\nids: ["x","y"]\nblock:\n  - "p"\n  - q\nempty:\n---\nbody'
    )
    assert (
        meta["title"] == "A: b"
        and meta["ids"] == ["x", "y"]
        and meta["block"] == ["p", "q"]
        and body == "body"
    )


def test_stream_maps_messages_to_threads(exp):
    d = store.exp_dir(exp)
    s = view.build_stream(d, "autonomous-drone@g.us")
    by = {m["id"]: m["thread"] for m in s["messages"]}
    assert by == {
        "m1": "20251013-flight-controller",
        "m2": "20251013-flight-controller",
        "m3": None,
        "m4": "20251015-esc-review",
        "m5": None,
    }
    assert s["unassigned"] == 2 and [t["title"] for t in s["threads"]] == ["Flight controller", "ESC review"]
    t0 = s["threads"][0]
    assert t0["messages"] == 2 and t0["summary"] == "Board arrived; PID tuning planned." and "ids" not in t0
    assert next(m for m in s["messages"] if m["id"] == "m3")["media"] == "image"


def test_channels_exclude_unset_and_count_messages(exp):
    assert view.list_channels(store.exp_dir(exp)) == [
        {"jid": "autonomous-drone@g.us", "title": "Autonomous Drone", "kind": "project", "messages": 5}
    ]


def test_api_surface_and_page_traversal_guard(exp):
    c = TestClient(app)
    assert c.get("/").status_code == 200 and "<title>testbench</title>" in c.get("/").text
    lst = c.get("/api/experiments").json()
    assert lst[0]["id"] == exp and lst[0]["state"] == "new"
    det = c.get(f"/api/experiments/{exp}").json()
    assert det["channels"][0]["messages"] == 5
    s = c.get(f"/api/experiments/{exp}/stream", params={"jid": "autonomous-drone@g.us"}).json()
    assert len(s["threads"]) == 2
    ok = c.get(
        f"/api/experiments/{exp}/page", params={"path": "channels/autonomous-drone/20251015-esc-review.md"}
    )
    assert ok.status_code == 200 and "ESC review" in ok.text
    assert c.get(f"/api/experiments/{exp}/page", params={"path": "../watcher.db"}).status_code == 404
    assert c.get(f"/api/experiments/{exp}/page", params={"path": "../../../etc/passwd"}).status_code == 404
    assert c.get("/api/experiments/exp-404").status_code == 404


def test_api_create_clone_reset_delete_and_errors(exp, tmp_path):
    c = TestClient(app)
    r = c.post("/api/experiments", json={"name": "b", "data_dir": str(SAMPLE), "env": {"BATCH_N": "9"}})
    new = r.json()["id"]
    cl = c.post(
        f"/api/experiments/{new}/clone", json={"name": "b2", "env": {"ASSIGNMENT_MODE": "jev"}}
    ).json()["id"]
    assert store.load_config(cl).env == {"BATCH_N": "9", "ASSIGNMENT_MODE": "jev"}
    bad = c.post(
        "/api/experiments", json={"name": "x", "data_dir": str(SAMPLE), "env": {"DATABASE_URL": "x"}}
    )
    assert bad.status_code == 400 and "pinned" in bad.json()["detail"]
    assert c.post("/api/experiments", json={"name": "x", "data_dir": "/nope"}).status_code == 400
    assert c.post(f"/api/experiments/{exp}/reset").status_code == 200
    assert not (store.exp_dir(exp) / "watcher.db").exists()
    assert c.delete(f"/api/experiments/{cl}").status_code == 200
    assert cl not in store.list_ids()


def test_stream_before_run_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOME", tmp_path)
    eid = store.create(ExperimentConfig(name="n", data_dir=str(SAMPLE)))
    c = TestClient(app)
    assert c.get(f"/api/experiments/{eid}/stream", params={"jid": "x"}).json()["messages"] == []
    assert c.get(f"/api/experiments/{eid}").json()["channels"] == []
