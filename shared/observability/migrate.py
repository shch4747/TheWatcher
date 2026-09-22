"""Additive schema migration for tables that already exist (ADR-0013).

The app has no Alembic - `Base.metadata.create_all` is the whole schema
story, and it only ever CREATEs, never ALTERs. That's fine for the new
`obs_*` tables, but the observability work also needs new columns on
`model_calls`, which already exists (with rows) in every deployed
`watcher.db`.

`ensure_columns` is the smallest thing that works: read the live column
list, add what's missing. SQLite's `ALTER TABLE ... ADD COLUMN` is O(1)
and safe on a populated table as long as the column is nullable or has a
constant default - both true here. Idempotent, so it runs on every
startup.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

# table -> column -> SQLite column type. Every column must be nullable
# (no NOT NULL without a default) so existing rows stay valid.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "model_calls": {
        "phase": "VARCHAR",
        "channel_jid": "VARCHAR",
        "ingest_run_id": "INTEGER",
        "duration_ms": "INTEGER",
        "outcome": "VARCHAR",
        "error": "TEXT",
        "attempt": "INTEGER",
        "cost_source": "VARCHAR",
    },
}


async def _existing_columns(conn: AsyncConnection, table: str) -> set[str]:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    return {row[1] for row in result}


async def ensure_columns(conn: AsyncConnection) -> list[str]:
    """Add any missing columns. Returns what it added, for logging and
    for the test that asserts a second run is a no-op."""
    added: list[str] = []
    for table, columns in ADDED_COLUMNS.items():
        present = await _existing_columns(conn, table)
        if not present:
            continue  # table doesn't exist yet; create_all just made it with every column
        for column, column_type in columns.items():
            if column in present:
                continue
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"))
            added.append(f"{table}.{column}")
    if added:
        logger.info("observability: added columns %s", ", ".join(added))
    return added
