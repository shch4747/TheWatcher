"""Cache of model answers, so re-running an experiment (or the UI re-opening one)
costs nothing, and comparisons differ only where *you* changed something.

`CachingClient` wraps a `TextModelClient`. The ingestion path only ever calls
`generate_structured` (and `generate`); both are cached. Key = model name +
system prompt + prompt + output schema. A hit returns the stored answer with
cost 0 and cost_source="cached" so the cost ledger (`model_calls`) doesn't
count it as spend; the original tokens/cost are kept in `saved_*` counters.

`shared.models.schemas` is imported lazily so this module can be unit-tested
without the Watcher repo.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


class CacheStore:
    def __init__(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, model TEXT, kind TEXT, value TEXT, "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )

    def get(self, key: str) -> dict | None:
        row = self._db.execute("SELECT value FROM llm_cache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, model: str, kind: str, value: dict) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO llm_cache(key, model, kind, value) VALUES (?,?,?,?)",
            (key, model, kind, json.dumps(value)),
        )

    def close(self) -> None:
        self._db.close()


def _key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class CachingClient:
    def __init__(self, inner: Any, store: CacheStore | None, label: str = "") -> None:
        self._inner, self._store, self.label = inner, store, label
        self.model_name = inner.model_name
        self.hits = self.misses = 0
        self.saved_input_tokens = self.saved_output_tokens = 0
        self.saved_cost = 0.0

    def __getattr__(
        self, name: str
    ) -> Any:  # model_settings etc.; generate_with_tools passes straight through
        return getattr(self._inner, name)

    def stats(self) -> dict:
        return {
            "label": self.label,
            "model": self.model_name,
            "hits": self.hits,
            "misses": self.misses,
            "saved_input_tokens": self.saved_input_tokens,
            "saved_output_tokens": self.saved_output_tokens,
            "saved_cost_usd": round(self.saved_cost, 6),
        }

    def _hit(self, cached: dict) -> None:
        self.hits += 1
        self.saved_input_tokens += cached.get("input_tokens", 0)
        self.saved_output_tokens += cached.get("output_tokens", 0)
        self.saved_cost += cached.get("cost") or 0.0

    async def generate(self, prompt: str, system: str | None = None):
        from shared.models.schemas import TextResult

        key = _key("text", self.model_name, system or "", prompt)
        cached = self._store.get(key) if self._store else None
        if cached:
            self._hit(cached)
            return TextResult(
                text=cached["text"], input_tokens=0, output_tokens=0, cost=0.0, cost_source="cached"
            )
        self.misses += 1
        res = await self._inner.generate(prompt, system=system)
        if self._store:
            self._store.put(
                key,
                self.model_name,
                "text",
                {
                    "text": res.text,
                    "input_tokens": res.input_tokens,
                    "output_tokens": res.output_tokens,
                    "cost": res.cost,
                },
            )
        return res

    async def generate_structured(self, prompt: str, output_type: type, system: str | None = None):
        from shared.models.schemas import StructuredResult

        key = _key(
            "structured",
            self.model_name,
            system or "",
            prompt,
            output_type.__name__,
            output_type.model_json_schema(),
        )
        cached = self._store.get(key) if self._store else None
        if cached:
            try:
                output = output_type.model_validate_json(cached["output"])
            except Exception:  # noqa: BLE001 - schema changed under the cache: treat as a miss
                cached = None
        if cached:
            self._hit(cached)
            return StructuredResult(
                output=output, input_tokens=0, output_tokens=0, cost=0.0, cost_source="cached"
            )
        self.misses += 1
        res = await self._inner.generate_structured(prompt, output_type, system=system)
        if self._store:
            self._store.put(
                key,
                self.model_name,
                "structured",
                {
                    "output": res.output.model_dump_json(),
                    "input_tokens": res.input_tokens,
                    "output_tokens": res.output_tokens,
                    "cost": res.cost,
                },
            )
        return res
