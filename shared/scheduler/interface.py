"""Scheduler interface (ADR-0004: its own module, ADR-0007: waits are rows).
Empty in Phase 0 — job registry, run ledger, triggers and locks land in
Phase 3. Declared now so other packages can import a stable name.
"""
from __future__ import annotations
