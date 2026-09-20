#!/usr/bin/env python3
"""Test the Lapis (or local vault) connection in isolation. Uses
LAPIS_TOKEN/LAPIS_BASE_URL/LAPIS_VAULT_ID if set, else falls back to a
local directory at VAULT_ROOT (default_vault_client() - the same
live-vs-stand-in choice the app itself makes).

Usage: uv run python scripts/check_lapis.py
"""
from __future__ import annotations

import asyncio
import json

from shared.config import settings
from shared.wiki.interface import LapisClient, check_vault_connection, default_vault_client


async def main() -> int:
    vault = default_vault_client()
    if isinstance(vault, LapisClient):
        kind = "LapisClient (live)"
    else:
        kind = "LocalDirClient (local, no LAPIS_TOKEN set)"
    result = await check_vault_connection(vault)
    print(f"backend: {kind}")
    if isinstance(vault, LapisClient):
        print(f"base_url: {settings.lapis_base_url}  vault_id: {settings.lapis_vault_id}")
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
