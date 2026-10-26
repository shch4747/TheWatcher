import asyncio
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel


# Stand-ins for shared.models.schemas (the real repo isn't importable in unit tests).
@dataclass
class TextResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None
    cost_source: str | None = None


@dataclass
class StructuredResult:
    output: object
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None
    cost_source: str | None = None


for name in ("shared", "shared.models", "shared.models.schemas"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["shared.models.schemas"].TextResult = TextResult
sys.modules["shared.models.schemas"].StructuredResult = StructuredResult

from testbench import store  # noqa: E402
from testbench.config import ExperimentConfig  # noqa: E402
from testbench.groups import DataError  # noqa: E402
from testbench.llm_cache import CacheStore, CachingClient  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "sample_data"


class Out(BaseModel):
    answer: str


class FakeInner:
    model_name = "fake/model"
    model_settings = {"x": 1}

    def __init__(self):
        self.calls = 0

    async def generate(self, prompt, system=None):
        self.calls += 1
        return TextResult(f"t:{prompt}", 10, 5, 0.01, "reported")

    async def generate_structured(self, prompt, output_type, system=None):
        self.calls += 1
        return StructuredResult(output_type(answer=prompt.upper()), 100, 20, 0.002, "reported")

    async def generate_with_tools(self, *a, **k):
        return "passthrough"


def run(coro):
    return asyncio.run(coro)


def test_structured_cache_hit_is_free_and_identical(tmp_path):
    store_ = CacheStore(tmp_path / "c.sqlite")
    inner = FakeInner()
    c = CachingClient(inner, store_)
    first = run(c.generate_structured("hello", Out, system="s"))
    second = run(c.generate_structured("hello", Out, system="s"))
    assert inner.calls == 1 and (c.hits, c.misses) == (1, 1)
    assert second.output == first.output == Out(answer="HELLO")
    assert second.cost == 0.0 and second.cost_source == "cached" and second.input_tokens == 0
    assert c.stats()["saved_input_tokens"] == 100 and c.stats()["saved_cost_usd"] == 0.002


def test_key_depends_on_prompt_system_and_model(tmp_path):
    store_ = CacheStore(tmp_path / "c.sqlite")
    inner = FakeInner()
    c = CachingClient(inner, store_)
    for prompt, system in (("a", "s"), ("b", "s"), ("a", "t")):
        run(c.generate_structured(prompt, Out, system=system))
    assert inner.calls == 3
    inner2 = FakeInner()
    inner2.model_name = "other/model"
    run(CachingClient(inner2, store_).generate_structured("a", Out, system="s"))
    assert inner2.calls == 1


def test_cache_persists_across_clients_and_text_path(tmp_path):
    path = tmp_path / "c.sqlite"
    inner = FakeInner()
    run(CachingClient(inner, CacheStore(path)).generate("x"))
    c2 = CachingClient(inner, CacheStore(path))
    r = run(c2.generate("x"))
    assert inner.calls == 1 and r.text == "t:x" and r.cost_source == "cached"


def test_disabled_cache_always_calls_and_passthrough(tmp_path):
    inner = FakeInner()
    c = CachingClient(inner, None)
    run(c.generate_structured("a", Out))
    run(c.generate_structured("a", Out))
    assert inner.calls == 2 and c.model_settings == {"x": 1}
    assert run(c.generate_with_tools()) == "passthrough"


def test_stale_cached_schema_is_a_miss(tmp_path):
    store_ = CacheStore(tmp_path / "c.sqlite")
    inner = FakeInner()
    c = CachingClient(inner, store_)
    run(c.generate_structured("a", Out))
    for row in store_._db.execute("SELECT key FROM llm_cache").fetchall():
        store_.put(
            row[0],
            "fake/model",
            "structured",
            {"output": '{"wrong": 1}', "input_tokens": 1, "output_tokens": 1},
        )
    run(c.generate_structured("a", Out))
    assert inner.calls == 2


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOME", tmp_path)
    return tmp_path


def _cfg(**kw):
    return ExperimentConfig(name="t", data_dir=str(SAMPLE), **kw)


def test_create_list_reset_clone_delete(home):
    a = store.create(_cfg(env={"BATCH_N": "20"}))
    b = store.create(_cfg())
    assert (a, b) == ("exp-001", "exp-002") and store.list_ids() == [a, b]
    d = store.exp_dir(a)
    (d / "vault").mkdir()
    (d / "vault" / "x.md").write_text("page")
    (d / "watcher.db").write_text("db")
    (d / "result.json").write_text("{}")
    assert store.has_outputs(a)
    store.reset(a)
    assert not store.has_outputs(a) and (d / "config.json").is_file()  # config survives reset
    c = store.clone(a, "variant", env={"ASSIGNMENT_MODE": "jev"}, date_from="2025-10-14")
    cc = store.load_config(c)
    assert (
        cc.name == "variant"
        and cc.env == {"BATCH_N": "20", "ASSIGNMENT_MODE": "jev"}
        and cc.date_from == "2025-10-14"
    )
    assert store.load_config(a).env == {"BATCH_N": "20"}  # original untouched
    store.delete(b)
    assert store.list_ids() == [a, c]
    with pytest.raises(KeyError):
        store.exp_dir("exp-099")


def test_env_isolation_and_safety(home):
    cfg = _cfg(env={"BATCH_N": "7"})
    d = home / "experiments" / "exp-001"
    d.mkdir(parents=True)
    env = store.build_env(cfg, d)
    assert env["BATCH_N"] == "7"
    assert env["DATABASE_URL"].endswith(str((d / "watcher.db").resolve()))
    assert env["VAULT_ROOT"] == str((d / "vault").resolve())
    assert (
        env["GOWA_BASE_URL"] == "http://127.0.0.1:9"
        and env["LAPIS_TOKEN"] == ""
        and env["OTEL_ENABLED"] == "false"
    )


@pytest.mark.parametrize(
    "env,needle",
    [
        ({"DATABASE_URL": "x"}, "pinned"),
        ({"GOWA_BASE_URL": "x"}, "pinned"),
        ({"LAPIS_TOKEN": "x"}, "pinned"),
        ({"MODELS_API_KEY": "sk-123"}, "secret"),
        ({"batch_n": "3"}, "UPPER_CASE"),
    ],
)
def test_config_rejects_dangerous_env(env, needle):
    with pytest.raises(DataError, match=needle):
        _cfg(env=env).validate()


def test_config_validation():
    with pytest.raises(DataError, match="after"):
        _cfg(date_from="2025-10-15", date_to="2025-10-01").validate()
    with pytest.raises(DataError, match="timezone"):
        _cfg(timezone="Mars/Olympus").validate()
    with pytest.raises(DataError, match="YYYY-MM-DD"):
        _cfg(date_from="15/10/2025").validate()
    _cfg(date_from="2025-10-01", date_to="2025-10-15", env={"ASSIGNMENT_MODE": "jev"}).validate()


def test_dead_runner_is_reported_as_crashed(home):
    exp = store.create(_cfg())
    (store.exp_dir(exp) / "status.json").write_text(json.dumps({"state": "running", "pid": 2_999_999}))
    assert store.status(exp)["state"] == "crashed"
    with pytest.raises(RuntimeError, match="already has results"):
        store.start(exp)
