"""
How the WA agent is wired for REAL operation (vs demo.py, which is mocks).

Runnable once gowa is up and paired (GOWA_BASE_URL reachable, a dedicated WA
number linked as a device) and its webhook points at THIS process:
    WHATSAPP_WEBHOOK=http://<this-host>:5000/webhook
    WHATSAPP_WEBHOOK_SECRET=<same as WA_WEBHOOK_SECRET below>

Memory: uses the real LapisAdapter when LAPIS_* env vars are set, else falls
back to MockWAMemory so you can smoke-test the gowa plumbing without a vault.

Env:
  GOWA_BASE_URL, GOWA_BASIC_AUTH, GOWA_DEVICE_ID   (gowa_client)
  WA_WEBHOOK_SECRET                                (must match gowa's secret)
  OPENROUTER_API_KEY                               (Kimi K2.6 classifier)
  LAPIS_BASE_URL, LAPIS_VAULT_ID, LAPIS_TOKEN      (real wiki; optional)
  WA_ALLOWLIST   comma-separated group JIDs to ingest

Run:  python run_live.py     (needs: pip install flask openai)
"""
import os

from memory_interface import MockWAMemory, MockDispatcher
from gowa_client import make_sender
from webhook import create_app, GroupBuffers
from pipeline import WAPipeline


def _build_memory():
    if os.environ.get("LAPIS_BASE_URL") and os.environ.get("LAPIS_VAULT_ID"):
        from lapis_client import HttpLapisClient
        from lapis_adapter import LapisAdapter
        print("memory: LapisAdapter (real wiki)")
        return LapisAdapter(HttpLapisClient())
    print("memory: MockWAMemory (set LAPIS_* to use the real wiki)")
    return MockWAMemory()


def _build_dispatcher(memory):
    # Queue triggers into the wiki when Lapis is live; else just log them.
    if os.environ.get("LAPIS_BASE_URL") and os.environ.get("LAPIS_VAULT_ID"):
        from lapis_client import HttpLapisClient
        from dispatcher import QueueDispatcher
        print("dispatcher: QueueDispatcher (writes triggers to the wiki)")
        return QueueDispatcher(HttpLapisClient())
    from dispatcher import LoggingDispatcher
    print("dispatcher: LoggingDispatcher (prints triggers)")
    return LoggingDispatcher()


def _build_registry(memory):
    """Group config (allowlist, per-group cadence, project->group mapping)
    lives in the wiki at meta/groups.md. Falls back to WA_ALLOWLIST env
    (all default cadence) when there's no registry / no wiki."""
    from group_context import GroupRegistry
    if os.environ.get("LAPIS_BASE_URL") and os.environ.get("LAPIS_VAULT_ID"):
        from lapis_client import HttpLapisClient
        reg = GroupRegistry.from_wiki(HttpLapisClient())
        if reg.groups:
            print(f"registry: {len(reg.groups)} group(s) from meta/groups.md")
            return reg
    jids = list(filter(None, os.environ.get("WA_ALLOWLIST", "").split(",")))
    print(f"registry: {len(jids)} group(s) from WA_ALLOWLIST env")
    return GroupRegistry.from_list([{"jid": j} for j in jids])


def main():
    memory = _build_memory()
    registry = _build_registry(memory)
    allowlist = registry.allowlist()
    if not allowlist:
        print("WARNING: no groups configured — nothing will be ingested. "
              "Set meta/groups.md in the wiki, or WA_ALLOWLIST.")

    dispatcher = _build_dispatcher(memory)
    sender = make_sender()           # rate-limited gowa sender

    pipeline = WAPipeline(
        memory=memory, dispatcher=dispatcher, allowlist=allowlist, sender=sender,
    )

    # NOTE: outbound flush + the reminder job run on their own schedule
    # (a scheduler is the remaining piece). Wire them as:
    #   pipeline.flush_notifications(registry)      # send queued agent outputs
    #   reminders.run_reminder_job(memory, default_audience="announce")
    buffers = GroupBuffers(registry=registry)
    app = create_app(pipeline, secret=os.environ.get("WA_WEBHOOK_SECRET", ""),
                     buffers=buffers, registry=registry)
    app.run(host="0.0.0.0", port=int(os.environ.get("WA_PORT", "5000")))


if __name__ == "__main__":
    main()
