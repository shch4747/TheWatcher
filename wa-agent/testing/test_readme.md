# Testing the WA agent

Everything here runs from the `testing/` folder. The files have a small path
shim at the top so `python testing/<file>.py` (run from `wa-agent/`) or
`python <file>.py` (run from inside `testing/`) both work.

There are two kinds of test:

1. **Unit tests** — offline, deterministic, no keys/network. Prove the logic.
2. **Connectivity checks** — hit the real Lapis + real gowa. Prove the wiring.

---

## 1. Unit tests (run these first, always)

No setup, no credentials:

```bash
cd wa-agent
python testing/test_wa_agent.py       # preprocessing, routing, pipeline   (13)
python testing/test_gowa.py           # webhook parse, signature, buffers   (11)
python testing/test_lapis.py          # LapisAdapter read/write/frontmatter (13)
python testing/test_dispatcher.py     # agent trigger queue                 (6)
python testing/test_group_context.py  # group registry + routing            (8)
```

Each prints `PASS/FAIL` per test and a total; exit code is non-zero on any
failure. All 51 should pass. (`pip install pytest` and `pytest -q testing/`
works too.)

`testing/test_lapis.py` uses `FakeLapisClient` — an in-memory vault — so it
exercises the real read/write/frontmatter logic **without** touching Lapis.

---

## 2. Test Lapis (the real vault)

You need a Lapis **bearer token** (see the main README / research-agent
README for how to get one — the Obsidian plugin's device-code flow, or the
`examples/` folder in the Lapis repo).

```bash
# PowerShell
$env:LAPIS_BASE_URL="https://lapis.dvenom.in"
$env:LAPIS_VAULT_ID="6adc07d5-b530-462f-be6d-cc399288bb78"
$env:LAPIS_BEARER_TOKEN="<your device bearer token>"

python testing/check_lapis.py
```

What it does, in order: lists the vault manifest (read), writes a throwaway
note under `WhatsApp/.state/`, reads it back, deletes it. If all four steps
pass, `LapisAdapter` will work against your vault.

- **401 / auth error** → token missing, wrong, or expired. It's the #1
  failure. A session cookie expires; prefer the device bearer token.
- **manifest OK but write fails** → the token can read but not write; check
  its scope.
- It prints your **real file list** — compare that against `LAPIS`/
  `INPUT_PAGES` in `lapis_adapter.py` and fix any path that doesn't match
  (e.g. if projects live somewhere other than `Projects/`).

> Tip: the research-agent has an even safer way to preview writes —
> `DRY_RUN=true` reads real data but logs writes instead of sending, and
> `LOCAL_MODE=true` swaps Lapis for a local folder entirely.

---

## 3. Test WhatsApp login (gowa)

**Stand up gowa** on the host (Pi/VPS), with a persistent session volume so
you only scan the QR once:

```bash
docker run --detach --publish=3000:3000 --name=whatsapp --restart=always \
  --volume=$(docker volume create --name=whatsapp):/app/storages \
  aldinokemal2104/go-whatsapp-web-multidevice rest \
  --basic-auth=admin:yourpassword \
  --webhook="http://localhost:5000/webhook" --webhook-secret="yoursecret"
```

**Pair it:** open `http://<host>:3000`, log in with the basic-auth, and scan
the QR from the **dedicated ARIES-Bot WhatsApp account** (WhatsApp →
Settings → Linked Devices → Link a Device). One scan; it reconnects on its
own after that.

**Add the bot** to your project + announcement groups (a group admin adds its
number, like adding any person).

**Then verify login + list group JIDs:**

```bash
# PowerShell
$env:GOWA_BASE_URL="http://localhost:3000"
$env:GOWA_BASIC_AUTH="admin:yourpassword"

python testing/check_gowa.py
```

It reports device/login status and lists your groups' JIDs (`...@g.us`).
Copy those into `meta/groups.md` (or `WA_ALLOWLIST`).

- **Connection refused** → gowa isn't running / wrong port.
- **401** → wrong `GOWA_BASIC_AUTH`.
- **No groups listed** → the bot hasn't been added to any group yet.

---

## 4. End-to-end smoke test (optional, once 2 & 3 pass)

With gowa paired and (optionally) `LAPIS_*` set, run the agent and send a
message in an allowlisted group:

```bash
pip install flask openai
python run_live.py            # from wa-agent/ ; serves POST /webhook on :5000
```

Send a test message like `paper: https://arxiv.org/abs/1234.5678` in a
project group. When that group's cadence trigger fires, you should see the
line appear in `WhatsApp/ResearchDigest.md` in the vault (and, with real
`LAPIS_*`, a trigger record under `inbox/triggers/research/`). Start with a
single low-cadence test group so you can watch one message flow through.
