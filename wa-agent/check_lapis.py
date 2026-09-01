"""
Lapis connectivity + permissions check. Run this FIRST, before wiring the
agent, to confirm your token and vault id actually work.

Usage (PowerShell):
    $env:LAPIS_BASE_URL="https://lapis.dvenom.in"
    $env:LAPIS_VAULT_ID="6adc07d5-b530-462f-be6d-cc399288bb78"
    $env:LAPIS_BEARER_TOKEN="<your device bearer token>"
    python check_lapis.py

It: (1) lists the manifest (read), (2) writes a tiny throwaway note,
(3) reads it back, (4) deletes it. If all four pass, LapisAdapter will work.
Nothing member-facing is touched — the test note lives under WhatsApp/.state/.
"""
import sys
from lapis_client import HttpLapisClient

TEST_PATH = "WhatsApp/.state/_connectivity_check.md"


def main() -> int:
    try:
        c = HttpLapisClient()
    except KeyError as e:
        print(f"FAIL: missing env var {e}. Set LAPIS_BASE_URL and LAPIS_VAULT_ID.")
        return 1

    auth = "bearer token" if c.token else ("session cookie" if c.cookie else "NONE (!)")
    print(f"base_url : {c.base_url}")
    print(f"vault_id : {c.vault_id}")
    print(f"auth     : {auth}")
    if auth.startswith("NONE"):
        print("FAIL: no LAPIS_BEARER_TOKEN or LAPIS_SESSION_COOKIE set.")
        return 1
    print("-" * 60)

    # 1. manifest (read)
    try:
        files = c.list_files("")
    except Exception as e:
        print(f"FAIL manifest/read: {e}")
        print("  -> usually a 401 (bad/expired token) or wrong vault id.")
        return 1
    print(f"[1/4] manifest OK — {len(files)} file(s). First few:")
    for p in sorted(files)[:15]:
        print(f"        {p}")
    print("      (^ this is your REAL vault layout — check LAYOUT/INPUT_PAGES against it)")

    # 2-4. write / read / delete round-trip
    try:
        c.write_file(TEST_PATH, "# connectivity check\nok\n")
        print(f"[2/4] write OK  -> {TEST_PATH}")
        back = c.read_file(TEST_PATH)
        if back and "connectivity check" in back:
            print("[3/4] read-back OK")
        else:
            print(f"[3/4] read-back MISMATCH: {back!r}")
            return 1
        c.delete_file(TEST_PATH)
        print("[4/4] delete OK" if c.read_file(TEST_PATH) is None else "[4/4] delete FAILED")
    except Exception as e:
        print(f"FAIL write/read/delete: {e}")
        print("  -> token can read but not write? check the token's scope.")
        return 1

    print("-" * 60)
    print("ALL GOOD — LapisAdapter will work. Paste me the file list above and")
    print("I'll confirm the WA agent's page paths match your real vault.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
