"""Characterization of main.py's job and hook wiring so a composition-root
refactor cannot silently drop a scheduled job or the chat/reaction hooks.
"""
from __future__ import annotations

import pytest
from shared.gateway import interface as gateway
from shared.inbox import interface as inbox
from shared.scheduler import interface as scheduler


@pytest.fixture(autouse=True)
def _reset_registries():
    gateway.clear_hooks()
    scheduler.unregister_all()
    inbox.unregister_all()
    yield
    gateway.clear_hooks()
    scheduler.unregister_all()
    inbox.unregister_all()


def test_setup_jobs_registers_hooks_jobs_and_inbox_consumer():
    from main import setup_jobs

    setup_jobs()

    assert len(gateway._message_hooks) == 1
    assert len(gateway._reaction_hooks) == 1
    assert len(gateway._health_lines) == 1

    jobs = scheduler.registered_jobs()
    assert jobs == [
        "expire_proposals_tick",
        "ingest_now",
        "ingest_tick",
        "lifecycle_tick",
        "project_agent_tick",
        "sunday_nudge_tick",
    ]
    assert scheduler.job_spec("ingest_now").lock_key == "ingest_tick"
    assert scheduler.job_spec("ingest_tick").lock_key == "ingest_tick"

    assert "project_agent" in inbox._consumers
