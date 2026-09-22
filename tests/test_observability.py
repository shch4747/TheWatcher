"""Observability acceptance criteria (ADR-0013): attribution survives
concurrency, cost is measured rather than guessed, every wiki mutation
is audited, and an ingest run reports per-channel time and per-phase
tokens/cost."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from agents.wa_agent import interface as wa_agent
from agents.wa_agent.assign import ItemOut
from shared.db import Channel, MessageBuffer, ModelCall, get_session
from shared.observability.interface import (
    CLASSIFICATION,
    COMPUTED,
    REPORTED,
    SUMMARISATION,
    UNKNOWN,
    ObservedVaultClient,
    current,
    ingest_run,
    record_model_call,
    resolve_cost,
    scope,
)
from shared.observability.migrate import ensure_columns
from shared.observability.models import IngestChannelRun, IngestRun, VaultOp
from shared.wiki.interface import ConflictError, LocalDirClient
from sqlalchemy import select, text

from tests.gowa_payloads import message_event
from tests.scripted_models import ScriptedStructuredWorker


@pytest.fixture
def vault(tmp_path: Path) -> LocalDirClient:
    return LocalDirClient(tmp_path)


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------


def test_scope_merges_and_restores():
    assert current().channel_jid is None
    with scope(channel_jid="c@g.us", job_name="ingest_tick"):
        assert current().channel_jid == "c@g.us"
        # an inner scope setting only the phase inherits the channel
        with scope(phase=CLASSIFICATION):
            assert current().phase == CLASSIFICATION
            assert current().channel_jid == "c@g.us"
            assert current().job_name == "ingest_tick"
        assert current().phase is None  # restored
    assert current().channel_jid is None


async def test_concurrent_tasks_do_not_share_context():
    """The property the whole design rests on: two channels being
    ingested in parallel must never attribute each other's model calls."""
    seen: dict[str, str | None] = {}

    async def worker(name: str) -> None:
        with scope(channel_jid=name):
            await asyncio.sleep(0)  # force interleaving
            seen[name] = current().channel_jid

    await asyncio.gather(worker("a@g.us"), worker("b@g.us"))
    assert seen == {"a@g.us": "a@g.us", "b@g.us": "b@g.us"}
    assert current().channel_jid is None


async def test_record_model_call_picks_up_ambient_attribution():
    with scope(channel_jid="attr@g.us", phase=SUMMARISATION, job_name="ingest_tick", ingest_run_id=77):
        await record_model_call("worker", "m", 10, 5, cost_usd=0.5, cost_source=REPORTED)

    async with get_session() as session:
        row = await session.scalar(
            select(ModelCall).where(ModelCall.channel_jid == "attr@g.us")
        )
    assert row is not None
    assert (row.phase, row.job_name, row.ingest_run_id) == (SUMMARISATION, "ingest_tick", 77)
    assert row.cost_usd == 0.5
    assert row.cost_source == REPORTED


# --------------------------------------------------------------------------
# cost
# --------------------------------------------------------------------------


@dataclass
class _Usage:
    """Enough of pydantic-ai's RequestUsage for the price path."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    input_audio_tokens: int = 0
    output_audio_tokens: int = 0
    cache_audio_read_tokens: int = 0
    cost: object = None
    requests: int = 1


def test_reported_cost_wins_over_computing_one():
    from decimal import Decimal

    cost, source = resolve_cost(
        _Usage(input_tokens=1000, output_tokens=500, cost=Decimal("0.0042")),
        "z-ai/glm-5.3-flash",
        "https://openrouter.ai/api/v1",
    )
    assert cost == pytest.approx(0.0042)
    assert source == REPORTED


def test_cost_is_computed_when_the_provider_reports_none():
    cost, source = resolve_cost(
        _Usage(input_tokens=1_000_000, output_tokens=1_000_000),
        "gpt-4o",
        "https://openrouter.ai/api/v1",
    )
    # Either genai-prices knows this model (computed, positive) or it
    # doesn't (unknown, None) - what must never happen is a crash or a
    # silent zero standing in for "we don't know".
    assert source in (COMPUTED, UNKNOWN)
    if source == COMPUTED:
        assert cost is not None and cost > 0
    else:
        assert cost is None


def test_unknown_model_returns_none_rather_than_raising():
    cost, source = resolve_cost(_Usage(input_tokens=10), "not-a-real-model-xyz", "http://example.invalid")
    assert (cost, source) == (None, UNKNOWN)


# --------------------------------------------------------------------------
# vault audit
# --------------------------------------------------------------------------


async def _vault_ops(path: str) -> list[VaultOp]:
    async with get_session() as session:
        rows = await session.scalars(select(VaultOp).where(VaultOp.path == path).order_by(VaultOp.id))
    return list(rows)


async def test_writes_and_deletes_are_audited_reads_are_not(vault: LocalDirClient):
    observed = ObservedVaultClient(vault)
    await observed.write("channels/a.md", "hello", base_revision="")
    await observed.read("channels/a.md")
    await observed.list("channels")
    await observed.delete("channels/a.md")

    ops = await _vault_ops("channels/a.md")
    assert [o.operation for o in ops] == ["write", "delete"]  # read/list not recorded
    assert ops[0].outcome == "ok"
    assert ops[0].content_bytes == 5
    assert ops[0].content_sha256 is not None
    assert ops[0].new_revision  # the revision the write produced


async def test_audit_is_transparent_to_the_caller(vault: LocalDirClient, tmp_path: Path):
    """Wrapping must not change what lands on disk or what's returned."""
    observed = ObservedVaultClient(vault)
    result = await observed.write("channels/b.md", "body", base_revision="")
    assert (tmp_path / "channels/b.md").read_text() == "body"
    assert (await observed.read("channels/b.md")).content == "body"
    assert result.revision == (await vault.read("channels/b.md")).revision


async def test_conflict_is_recorded_and_still_raised(vault: LocalDirClient):
    observed = ObservedVaultClient(vault)
    await observed.write("channels/c.md", "first", base_revision="")
    with pytest.raises(ConflictError):
        await observed.write("channels/c.md", "second", base_revision="stale-revision")

    ops = await _vault_ops("channels/c.md")
    assert [o.outcome for o in ops] == ["ok", "conflict"]
    assert ops[1].error


async def test_vault_ops_carry_the_ambient_channel():
    """A write during a channel's batch is attributable to that channel."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        observed = ObservedVaultClient(LocalDirClient(Path(tmp)))
        with scope(channel_jid="vault-attr@g.us", phase=SUMMARISATION):
            await observed.write("channels/d.md", "x", base_revision="")
    ops = await _vault_ops("channels/d.md")
    assert ops[0].channel_jid == "vault-attr@g.us"
    assert ops[0].phase == SUMMARISATION


# --------------------------------------------------------------------------
# migration
# --------------------------------------------------------------------------


async def test_ensure_columns_is_additive_and_idempotent():
    from shared.db import _engine

    async with _engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS obs_migrate_probe"))
        # stand-in for a model_calls table that predates the new columns
        await conn.execute(text("CREATE TABLE obs_migrate_probe (id INTEGER PRIMARY KEY, tier TEXT)"))
        await conn.execute(text("INSERT INTO obs_migrate_probe (tier) VALUES ('worker')"))

    from shared.observability import migrate

    original = migrate.ADDED_COLUMNS
    migrate.ADDED_COLUMNS = {"obs_migrate_probe": {"phase": "VARCHAR", "duration_ms": "INTEGER"}}
    try:
        async with _engine.begin() as conn:
            added = await ensure_columns(conn)
            assert set(added) == {"obs_migrate_probe.phase", "obs_migrate_probe.duration_ms"}
            # the pre-existing row survived
            count = await conn.execute(text("SELECT COUNT(*) FROM obs_migrate_probe"))
            assert count.scalar() == 1

        async with _engine.begin() as conn:
            assert await ensure_columns(conn) == []  # second run is a no-op
    finally:
        migrate.ADDED_COLUMNS = original
        async with _engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS obs_migrate_probe"))


async def test_model_calls_has_the_attribution_columns_live():
    """init_db (the autouse fixture) must have brought the real table up
    to date, not just created it for fresh databases."""
    from shared.db import _engine

    async with _engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(model_calls)"))
        columns = {row[1] for row in result}
    assert {"phase", "channel_jid", "ingest_run_id", "duration_ms", "cost_source"} <= columns


# --------------------------------------------------------------------------
# ingest runs + report
# --------------------------------------------------------------------------


async def _seed(jid: str, title: str, texts: list[str], kind: str = "project") -> list[str]:
    ids = [f"{title.lower()}-m{i}" for i in range(len(texts))]
    async with get_session() as session:
        session.add(Channel(jid=jid, kind=kind, title=title, initiative=title))
        for message_id, body in zip(ids, texts, strict=True):
            session.add(
                MessageBuffer(
                    message_id=message_id, channel=jid, event_type="message",
                    payload=json.dumps(message_event(message_id, jid, body, sender="a@x")),
                )
            )
        await session.commit()
    return ids


async def test_ingest_run_records_per_channel_time_and_phase_split(
    vault: LocalDirClient, monkeypatch: pytest.MonkeyPatch
):
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 1)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    jid = "obs-run@g.us"
    ids = await _seed(jid, "ObsRun", ["book the hall"])
    worker = ScriptedStructuredWorker(
        items=[ItemOut(kind="task", text="Book the hall", src_ids=[ids[0]])]
    )

    async with get_session() as session:
        channel = await session.scalar(select(Channel).where(Channel.jid == jid))

    async with ingest_run() as run:
        async with run.channel(channel):
            run.record(await wa_agent.run_batch(jid, vault, worker))

    async with get_session() as session:
        channel_row = await session.scalar(
            select(IngestChannelRun).where(IngestChannelRun.channel_jid == jid)
        )
        run_row = await session.get(IngestRun, run.run_id)

    assert channel_row is not None
    assert channel_row.duration_ms is not None and channel_row.duration_ms >= 0
    assert channel_row.messages == 1
    assert channel_row.threads_created == 1
    # both ingestion phases were exercised and attributed separately
    assert channel_row.classification_calls == 1
    assert channel_row.summarisation_calls == 1
    assert channel_row.classification_input_tokens > 0
    assert channel_row.summarisation_input_tokens > 0
    assert channel_row.model_calls == 2

    assert run_row is not None
    assert run_row.messages == 1
    assert run_row.channels_with_work == 1
    assert run_row.outcome == "success"
    assert run_row.duration_ms is not None

    report = run.report()
    assert report is not None
    assert "ObsRun" in report
    assert "classify" in report and "summarise" in report


async def test_no_op_run_writes_a_row_but_reports_nothing(vault: LocalDirClient):
    """The tick fires every 2 minutes and mostly finds nothing - those
    runs must not reach the logs channel."""
    jid = "obs-quiet@g.us"
    async with get_session() as session:
        session.add(Channel(jid=jid, kind="project", title="Quiet"))
        await session.commit()
        channel = await session.scalar(select(Channel).where(Channel.jid == jid))

    async with ingest_run() as run:
        async with run.channel(channel):
            run.record(await wa_agent.run_batch(jid, vault, ScriptedStructuredWorker()))

    assert run.report() is None
    async with get_session() as session:
        row = await session.get(IngestRun, run.run_id)
    assert row is not None and row.messages == 0  # recorded, just not announced


async def test_failing_channel_is_recorded_and_the_error_propagates(vault: LocalDirClient):
    jid = "obs-boom@g.us"
    async with get_session() as session:
        session.add(Channel(jid=jid, kind="project", title="Boom"))
        await session.commit()
        channel = await session.scalar(select(Channel).where(Channel.jid == jid))

    with pytest.raises(RuntimeError):
        async with ingest_run() as run:
            async with run.channel(channel):
                raise RuntimeError("provider exploded")

    async with get_session() as session:
        channel_row = await session.scalar(
            select(IngestChannelRun).where(IngestChannelRun.channel_jid == jid)
        )
        run_row = await session.get(IngestRun, run.run_id)
    assert channel_row is not None and channel_row.outcome == "failure"
    assert "provider exploded" in (channel_row.error or "")
    assert run_row is not None and run_row.outcome == "failure"


def test_report_formats_money_and_tokens_for_a_phone():
    from shared.observability.interface import fmt_cost, fmt_duration, fmt_tokens

    # a run costs fractions of a cent; 2dp would render every run as $0.00
    assert fmt_cost(0.0041) == "$0.0041"
    assert fmt_cost(12.4) == "$12.40"
    assert fmt_cost(None) == "n/a"
    assert fmt_tokens(18_200) == "18.2k"
    assert fmt_tokens(950) == "950"
    assert fmt_duration(12_400) == "12.4s"
    assert fmt_duration(95_000) == "1m35s"
    assert fmt_duration(None) == "0s"


async def test_failed_model_calls_are_logged_not_lost():
    """A call that raises used to leave no row at all, so a burned
    provider request was invisible to the cost dashboard."""
    from shared.models.calls import generate
    from shared.models.text import TextModelClient

    class Exploding(TextModelClient):
        def __init__(self):
            self.model_name = "exploding-model"

        async def generate(self, prompt, system=None):  # type: ignore[override]
            raise RuntimeError("provider 500")

    with pytest.raises(RuntimeError):
        await generate(Exploding(), "worker", "hi")

    async with get_session() as session:
        row = await session.scalar(
            select(ModelCall).where(ModelCall.model_name == "exploding-model")
        )
    assert row is not None
    assert row.outcome == "error"
    assert "provider 500" in (row.error or "")


async def test_ingest_run_ids_are_attributed_to_model_calls(vault: LocalDirClient, monkeypatch):
    """Rollups read model_calls back by ingest_run_id, so the id must
    actually reach the rows."""
    from shared.config import settings

    monkeypatch.setattr(settings, "batch_n", 1)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    jid = "obs-attr2@g.us"
    await _seed(jid, "Attr2", ["hello there"])
    async with get_session() as session:
        channel = await session.scalar(select(Channel).where(Channel.jid == jid))

    async with ingest_run() as run:
        async with run.channel(channel):
            run.record(await wa_agent.run_batch(jid, vault, ScriptedStructuredWorker()))

    async with get_session() as session:
        rows = list(
            await session.scalars(select(ModelCall).where(ModelCall.ingest_run_id == run.run_id))
        )
    assert rows
    assert {r.phase for r in rows} == {CLASSIFICATION, SUMMARISATION}
    assert all(r.channel_jid == jid for r in rows)
    assert all(r.duration_ms is not None for r in rows)


async def test_logs_channel_is_reported_as_excluded_not_as_threads(vault: LocalDirClient, monkeypatch):
    from shared.config import settings
    from sqlalchemy import delete

    monkeypatch.setattr(settings, "batch_n", 1)
    monkeypatch.setattr(settings, "batch_quiet_minutes", 0)

    jid = "obs-logs@g.us"
    await _seed(jid, "ObsLogs", ["⚠️ ingest_tick failed"], kind="logs")
    try:
        async with get_session() as session:
            channel = await session.scalar(select(Channel).where(Channel.jid == jid))
        async with ingest_run() as run:
            async with run.channel(channel):
                run.record(await wa_agent.run_batch(jid, vault, ScriptedStructuredWorker()))

        report = run.report()
        assert report is not None
        assert "not ingested (logs channel)" in report
    finally:
        # `logs` is a singleton kind and the test DB is shared
        async with get_session() as session:
            await session.execute(delete(Channel).where(Channel.jid == jid))
            await session.commit()


def test_old_and_new_timestamps_render_in_ist():
    """`/health` is read on a phone in Delhi; SQLite hands back naive
    datetimes that were written as UTC."""
    from shared.gateway.interface import _isoformat

    assert _isoformat(datetime(2026, 9, 22, 13, 16, 7, tzinfo=UTC)) == "2026-09-22 18:46 IST"
    assert _isoformat(datetime(2026, 9, 22, 13, 16, 7)) == "2026-09-22 18:46 IST"
    # a +05:30 offset is already IST and must not shift again
    ist = datetime(2026, 9, 22, 18, 46, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert _isoformat(ist) == "2026-09-22 18:46 IST"
    assert _isoformat(None) == "unknown"
