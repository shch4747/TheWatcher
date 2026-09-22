#!/usr/bin/env python3
"""Mark pre-ADR-0013 `model_calls` rows so dashboards can exclude them.

Two kinds of rows in a database that predates the observability work
would otherwise skew every cost panel:

1. **Bogus Jev rows.** `decide_with_fallback` used to write a hardcoded
   `ModelCall("decision", "jev", 0, 0)` on every call - even when the
   client was `WorkerBackedDecisionModel`, which logged its own real
   worker call separately. Those rows are a double count with zero
   tokens and no cost. On the live database at the time of writing they
   were ~550 of ~1100 rows.
2. **Everything else from before the cutover**, which has no `phase`,
   no `channel_jid` and no `cost_usd` - real calls, but unattributable.

Neither is deleted: history is history. They get `phase='legacy'` so a
panel can say `WHERE phase != 'legacy'` and mean it.

Usage:
    uv run python scripts/backfill_observability.py          # dry run
    uv run python scripts/backfill_observability.py --yes    # apply
"""
from __future__ import annotations

import asyncio
import sys

from shared.db import ModelCall, get_session, init_db
from sqlalchemy import func, select, update

LEGACY = "legacy"


async def main(apply: bool) -> None:
    await init_db()

    async with get_session() as session:
        total = await session.scalar(select(func.count()).select_from(ModelCall))
        # Rows the old code wrote: no phase (never set before ADR-0013).
        unattributed = await session.scalar(
            select(func.count()).select_from(ModelCall).where(ModelCall.phase.is_(None))
        )
        bogus = await session.scalar(
            select(func.count())
            .select_from(ModelCall)
            .where(
                ModelCall.phase.is_(None),
                ModelCall.tier == "decision",
                ModelCall.model_name == "jev",
                ModelCall.input_tokens == 0,
                ModelCall.output_tokens == 0,
            )
        )

    print(f"model_calls rows:        {total}")
    print(f"  unattributed (no phase): {unattributed}")
    print(f"  of which bogus jev rows: {bogus}")

    if not unattributed:
        print("\nNothing to backfill.")
        return

    if not apply:
        print(f"\nDry run. Re-run with --yes to mark {unattributed} row(s) phase='{LEGACY}'.")
        return

    async with get_session() as session:
        await session.execute(
            update(ModelCall).where(ModelCall.phase.is_(None)).values(phase=LEGACY)
        )
        await session.commit()
    print(f"\nMarked {unattributed} row(s) phase='{LEGACY}'.")
    print("Dashboards filter these out with: WHERE phase != 'legacy'")


if __name__ == "__main__":
    asyncio.run(main(apply="--yes" in sys.argv))
