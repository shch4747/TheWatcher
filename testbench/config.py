"""An experiment = a JSON config + its own folder. No `shared/` imports."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from testbench.dataset import date_bounds
from testbench.groups import DataError

# The runner pins these itself; letting a config override them could point an
# experiment at the real database/wiki/WhatsApp.
_FORBIDDEN_PREFIXES = ("DATABASE_URL", "VAULT_ROOT", "LAPIS_", "GOWA_", "OTEL_")
_SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


@dataclass
class ExperimentConfig:
    name: str
    data_dir: str  # folder with groups.csv, exports.csv, members.csv
    groups: list[str] | None = None  # None = every enabled group
    date_from: str | None = None  # inclusive, local date YYYY-MM-DD
    date_to: str | None = None  # inclusive
    timezone: str = "Asia/Kolkata"
    env: dict[str, str] = field(
        default_factory=dict
    )  # Settings overrides, e.g. {"ASSIGNMENT_MODE": "jev", "BATCH_N": "20"}
    cache: bool = True  # reuse stored LLM answers (identical prompt+model => free, deterministic)
    include_media: bool = False  # feed media-only messages (empty text) to the agent
    lifecycle_days_after: float | None = (
        None  # after replay, run the stale/archive pass at last message + N days
    )
    seed_vault: str | None = None  # folder copied in as the starting wiki (optional)
    notes: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def validate(self) -> None:
        if not self.name.strip():
            raise DataError("experiment needs a name")
        if not Path(self.data_dir).is_dir():
            raise DataError(f"data_dir {self.data_dir} is not a folder")
        try:
            ZoneInfo(self.timezone)
        except Exception:  # noqa: BLE001
            raise DataError(f"unknown timezone {self.timezone!r}") from None
        date_bounds(self.date_from, self.date_to, self.timezone)
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise DataError("date_from is after date_to")
        for key, value in self.env.items():
            if key != key.upper() or not key.replace("_", "").isalnum():
                raise DataError(f"env key {key!r}: use UPPER_CASE setting names")
            if key.startswith(_FORBIDDEN_PREFIXES):
                raise DataError(f"env key {key} is pinned by the testbench and cannot be overridden")
            if any(w in key for w in _SECRET_WORDS):
                raise DataError(
                    f"env key {key} looks like a secret - keep it in the environment/.env, not in config.json"
                )
            if not isinstance(value, str):
                raise DataError(f"env {key} must be a string")
        if self.seed_vault and not Path(self.seed_vault).is_dir():
            raise DataError(f"seed_vault {self.seed_vault} is not a folder")

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def load(cls, path: Path) -> ExperimentConfig:
        raw = json.loads(Path(path).read_text())
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})
