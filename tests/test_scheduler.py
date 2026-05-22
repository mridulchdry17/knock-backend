"""Tests for the autopilot scheduler wiring (app/jobs/scheduler.py).

We do NOT spin up real background threads or let the cycle run against the DB —
the cycle itself is covered in test_autopilot_cycle_cron.py. Here we only verify:
  - build_scheduler registers the cycle job on the configured interval
  - the job target swallows exceptions (one bad cycle can't kill the thread)
  - start_scheduler respects the RUN_SCHEDULER flag (no-op when off)
"""
from __future__ import annotations

from unittest.mock import patch

from app.jobs import scheduler as sched


def test_build_scheduler_registers_cycle_job() -> None:
    s = sched.build_scheduler()
    try:
        job = s.get_job("autopilot_cycle")
        assert job is not None
        assert job.max_instances == 1
        # IntervalTrigger reflects the configured cadence.
        from app.config import settings

        assert (
            job.trigger.interval.total_seconds()
            == settings.AUTOPILOT_CYCLE_INTERVAL_MINUTES * 60
        )
    finally:
        # Built but never started; nothing to shut down, but be defensive.
        if s.running:
            s.shutdown(wait=False)


def test_run_cycle_safe_swallows_exceptions() -> None:
    """A failing cycle must not propagate out of the job target."""
    with patch(
        "app.jobs.autopilot_cycle_cron.run_cycle",
        side_effect=RuntimeError("boom"),
    ):
        # Should not raise — the job target catches and logs.
        sched._run_cycle_safe()


def test_start_scheduler_noop_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(sched.settings, "RUN_SCHEDULER", False)
    # Ensure clean state.
    sched.shutdown_scheduler()
    sched.start_scheduler()
    assert sched._scheduler is None


def test_start_scheduler_starts_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(sched.settings, "RUN_SCHEDULER", True)
    try:
        sched.start_scheduler()
        assert sched._scheduler is not None
        assert sched._scheduler.running
        # Idempotent: a second call doesn't replace the running scheduler.
        first = sched._scheduler
        sched.start_scheduler()
        assert sched._scheduler is first
    finally:
        sched.shutdown_scheduler()
        assert sched._scheduler is None
