#!/usr/bin/env python3
"""Test the gowa connection in isolation (reads GOWA_* from .env/env).

Usage: uv run python scripts/check_gowa.py
"""
from __future__ import annotations

import asyncio
import json

from shared.gateway.interface import check_gowa_connection


async def main() -> int:
    result = await check_gowa_connection()
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
