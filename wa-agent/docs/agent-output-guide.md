# How to Send WhatsApp Messages from Your Agent

## The Short Version

To send a message to any WhatsApp group, your agent writes an entry to **its own output file** on Lapis:

```
WhatsApp/output/<your-agent-name>.md
```

For example:
- Research agent → `WhatsApp/output/research.md`
- Innovation agent → `WhatsApp/output/innovation.md`
- Project agent → `WhatsApp/output/project.md`

The WA agent polls all files under `WhatsApp/output/` every ~2 minutes, picks up your entries, frames them into contextual WhatsApp messages, and sends via gowa. You never touch gowa or WhatsApp directly.

**Why per-agent files?** Each agent writes only to its own file, eliminating cross-agent write contention. No more read-modify-write races on a shared file — your reads and writes are always against your own state.

> **Migration note:** The legacy single-file path `WhatsApp/outputmessages.md` is still read by the WA agent for backward compatibility, but new entries should go to the per-agent paths.

## File Format

Each agent's output file uses YAML frontmatter with a `queue` list. Each entry in the list is one pending message:

```markdown
---
type: output-queue
queue:
  - id: "research-1725544200"
    source_agent: "research"
    audience: "project:dashboard"
    payload: "Found 3 new retrieval-augmented generation papers this week, including Self-RAG which is directly relevant to our pipeline."
    urgency: "normal"
    created_at: "2026-09-05T14:30:00+00:00"
    source_ids: []
    sent: false
---
# Output Messages Queue

Managed by TheWatcher. Do not edit manually.
```

## Field Reference

| Field | Required | Description |
|-------|----------|-------------|
| `id` | Yes | A unique string (UUID or `<agent>-<timestamp>`). Used for dedup and tracking. |
| `source_agent` | Yes | Your agent name: `"research"`, `"innovation"`, or `"project"`. |
| `audience` | Yes | **Where** to send it (see Audience Routing below). |
| `payload` | Yes | The actual content to share. Write it as a clear summary — the WA agent may rephrase it for the target group's context. |
| `urgency` | No | `"normal"` (default) or `"high"`. High-urgency items are sent immediately rather than batched. |
| `created_at` | No | ISO 8601 timestamp. Defaults to now if omitted. |
| `source_ids` | No | List of original WhatsApp message IDs that triggered this output, for traceability. |
| `sent` | No | Always set to `false` when you write it. The WA agent flips this to `true` after sending. |

## Audience Routing

The `audience` field determines which WhatsApp group(s) receive the message:

| Audience value | Resolves to |
|----------------|-------------|
| `"project:dashboard"` | The group registered for the `dashboard` project |
| `"project:watcher"` | The group registered for the `watcher` project |
| `"announce"` | The ARIES TLDR / announcement group(s) |
| `"events"` | The ARIES Events group |
| `"coordi"` | The coordinator chat group |
| `"exes"` or `"executives"` | The executive chat group |
| `"120363XXX@g.us"` | A specific WhatsApp group JID (direct) |
| Any bare project name | Looked up in the project registry, falls back to announce |

**When in doubt, use `"announce"`** — it goes to the TLDR group where everyone sees it.

## How to Write (Lapis API)

Use a **read-modify-write** pattern on your agent's own file. In pseudocode:

```
1. Read  WhatsApp/output/<your-agent>.md  from Lapis
2. Parse the YAML frontmatter to get the queue list
3. Append your new entry to the queue list
4. Write the file back with the updated frontmatter
```

### TypeScript (Research Agent)

```typescript
import { readFile, writeFile } from './lapis';  // your Lapis client
import { parseFrontmatter, dumpFrontmatter } from './mdfront';

const OUTPUT_PATH = 'WhatsApp/output/research.md';

async function sendToWhatsApp(payload: string, audience: string) {
  const raw = await readFile(OUTPUT_PATH);
  const { meta, body } = parseFrontmatter(raw || '---\ntype: output-queue\nqueue: []\n---\n');

  const queue = Array.isArray(meta.queue) ? meta.queue : [];
  queue.push({
    id: `research-${Date.now()}`,
    source_agent: 'research',
    audience,
    payload,
    urgency: 'normal',
    created_at: new Date().toISOString(),
    source_ids: [],
    sent: false,
  });

  meta.queue = queue;
  await writeFile(OUTPUT_PATH,
    dumpFrontmatter(meta, body || '# Output Messages Queue\n'));
}
```

### Python (Innovation / Project Agent)

```python
import mdfront  # from wa-agent/mdfront.py — copy it or reimplement the 20-line parser
from lapis_client import HttpLapisClient

AGENT_NAME = "innovation"  # or "project"
OUTPUT_PATH = f"WhatsApp/output/{AGENT_NAME}.md"

def send_to_whatsapp(client, payload: str, audience: str):
    raw = client.read_file(OUTPUT_PATH)
    if raw:
        meta, body = mdfront.parse(raw)
    else:
        meta = {"type": "output-queue", "queue": []}
        body = "# Output Messages Queue\n"

    queue = meta.get("queue", []) or []
    queue.append({
        "id": f"{AGENT_NAME}-{int(time.time())}",
        "source_agent": AGENT_NAME,
        "audience": audience,
        "payload": payload,
        "urgency": "normal",
        "created_at": datetime.utcnow().isoformat() + "Z",
        "source_ids": [],
        "sent": False,
    })

    meta["queue"] = queue
    client.write_file(OUTPUT_PATH, mdfront.dump(meta, body))
```

## Important Notes

1. **Each agent writes only to its own file** — `WhatsApp/output/<your-agent>.md`. Never write to another agent's file.

2. **Idempotency** — use a stable, unique `id`. If your agent crashes mid-write and retries, the WA agent deduplicates on `id`.

3. **Payload is content, not formatting** — write the substance of what you want to share. The WA agent will frame it with context like "[from Research agent]" and may rephrase for the target group. Don't add your own "[Research]" prefix or emoji — the WA agent handles presentation.

4. **Concurrency** — since each agent has its own file, there's no cross-agent contention. You only need to worry about concurrent writes within your own agent (if it has multiple workers).

5. **Flush cadence** — the WA agent checks output files every ~2 minutes. High-urgency items are sent on the next check; normal items may be batched together if multiple are pending for the same group.

6. **Cleanup** — the WA agent handles cleanup: it marks items `sent: true` after sending and prunes old sent items (keeps last 50 per file for audit trail).

## What Your Agent Reads (Input Pages)

The WA agent also writes to per-agent input pages when it finds something relevant in chat:

| Agent | Input page | What's there |
|-------|-----------|--------------|
| Research | `WhatsApp/ResearchDigest.md` | Papers, arxiv links, research mentions from chat |
| Innovation | `Ideas/Inbox.md` | Project ideas, event ideas, feedback from chat |
| Project | `Projects/<name>.md` | Project updates, tasks extracted from chat |

Your agent should **read and consume** these on each run (clear the entries you've processed). The WA agent appends; you consume.
