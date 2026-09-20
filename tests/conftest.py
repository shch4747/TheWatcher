"""Test env must be set before `shared.config`/`shared.db` are first
imported (Settings() reads the environment at import time)."""
import os
import tempfile

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmp_db.name}")
os.environ.setdefault("GOWA_WEBHOOK_SECRET", "test-secret")
