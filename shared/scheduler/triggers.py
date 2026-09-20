"""Trigger types (Spec: "Scheduler"). No workflow DSL - these are the four
named kinds and nothing else; `run_after` is event-driven so it has no
`is_due` polling behaviour, it's fired directly by the emitting code.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class RunNow:
    """Due immediately, once, the first time it's checked."""


@dataclass(frozen=True)
class RunAt:
    when: datetime


@dataclass(frozen=True)
class RunEvery:
    interval: timedelta


@dataclass(frozen=True)
class RunAfter:
    event: str


Trigger = RunNow | RunAt | RunEvery | RunAfter


def is_due(trigger: Trigger, last_finished_at: datetime | None, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    if isinstance(trigger, RunNow):
        return last_finished_at is None
    if isinstance(trigger, RunAt):
        return last_finished_at is None and now >= trigger.when
    if isinstance(trigger, RunEvery):
        return last_finished_at is None or now - last_finished_at >= trigger.interval
    if isinstance(trigger, RunAfter):
        return False  # fired directly by shared.scheduler.interface.run_after_event
    raise TypeError(f"unknown trigger type: {trigger!r}")
