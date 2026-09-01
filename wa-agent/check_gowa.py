"""
gowa / WhatsApp login check. Run this after gowa is up and you've scanned
the QR, to confirm the session is live and to list your group JIDs (which
you'll put in meta/groups.md / WA_ALLOWLIST).

Usage:
    $env:GOWA_BASE_URL="http://localhost:3000"
    $env:GOWA_BASIC_AUTH="admin:yourpassword"
    python check_gowa.py
"""
import base64
import json
import os
import sys
import urllib.request
import urllib.error


def _get(path: str):
    base = os.environ.get("GOWA_BASE_URL", "http://localhost:3000").rstrip("/")
    auth = os.environ.get("GOWA_BASIC_AUTH", "")
    headers = {}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    req = urllib.request.Request(f"{base}{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "{}")


def main() -> int:
    try:
        print("Checking devices / login status ...")
        try:
            print(json.dumps(_get("/app/devices"), indent=2)[:1500])
        except urllib.error.HTTPError as e:
            print(f"  /app/devices -> HTTP {e.code} (some builds differ; continuing)")

        print("\nYour groups (put these JIDs in the allowlist):")
        groups = _get("/user/my/groups")
        data = groups.get("results", groups) if isinstance(groups, dict) else groups
        data = (data or {}).get("data", data) if isinstance(data, dict) else data
        if not data:
            print("  (none — is the bot added to any groups yet?)")
        else:
            for g in (data if isinstance(data, list) else [data]):
                jid = g.get("JID") or g.get("jid") or g.get("id")
                name = g.get("Name") or g.get("name") or g.get("subject") or ""
                print(f"  {jid}   {name}")
        print("\nIf you see your groups above, WhatsApp login works.")
        return 0
    except urllib.error.HTTPError as e:
        print(f"FAIL: HTTP {e.code} — {e.read().decode(errors='replace')[:300]}")
        print("  401 -> check GOWA_BASIC_AUTH. Connection refused -> gowa not running.")
        return 1
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        print("  Is gowa running at GOWA_BASE_URL and paired (QR scanned)?")
        return 1


if __name__ == "__main__":
    sys.exit(main())
