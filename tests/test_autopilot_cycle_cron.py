"""Smoke test for the B5.7 autopilot cycle cron entry point.

Exercises the three-phase orchestration (batch_gen → send_worker →
reply_ingestor) at the module boundary. Each downstream phase is mocked
so we verify the cycle just sequences them correctly and surfaces a
combined summary.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch


@dataclass
class _FakeBatchResult:
    user_id: int
    items_created: int
    items_skipped: int = 0
    reason_if_skipped: str | None = None


@dataclass
class _FakeDrain:
    attempted: int
    sent: int
    failed: int
    skipped: int
    failures_by_kind: dict


@dataclass
class _FakeIngest:
    user_id: int
    processed: int
    replies_matched: int
    explicit_stops: int
    user_reply_locks_written: int
    error_kind: str | None = None


def test_run_cycle_aggregates_all_three_phases(engine, monkeypatch) -> None:
    """run_cycle() should call each phase once and surface a summary that
    sums their outputs. Tests against the real SessionLocal indirectly via
    mocking SessionLocal to return our test engine's session."""
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    from app.jobs import autopilot_cycle_cron as cron

    fake_batch = [
        _FakeBatchResult(user_id=1, items_created=3),
        _FakeBatchResult(user_id=2, items_created=2),
    ]
    fake_drain = _FakeDrain(attempted=5, sent=4, failed=1, skipped=0, failures_by_kind={"transient": 1})
    fake_ingest = [
        _FakeIngest(user_id=1, processed=2, replies_matched=2, explicit_stops=1, user_reply_locks_written=1),
        _FakeIngest(user_id=2, processed=0, replies_matched=0, explicit_stops=0, user_reply_locks_written=0),
    ]

    with patch.object(cron, "SessionLocal", factory), patch.object(
        cron.batch_gen, "generate_batch_for_all_users", return_value=fake_batch
    ), patch.object(
        cron.send_worker, "drain_due_items", return_value=fake_drain
    ), patch.object(
        cron.reply_ingestor, "ingest_replies_for_all_users", return_value=fake_ingest
    ):
        result = cron.run_cycle()

    assert result.batch_users_processed == 2
    assert result.batch_items_created == 5
    assert result.sent == 4
    assert result.failed == 1
    assert result.skipped_sends == 0
    assert result.ingest_users_processed == 2
    assert result.replies_matched == 2
    assert result.explicit_stops == 1


def test_run_cycle_handles_zero_users(engine) -> None:
    """An empty system: no users → no batches, nothing to drain, no ingest."""
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    from app.jobs import autopilot_cycle_cron as cron

    with patch.object(cron, "SessionLocal", factory), patch.object(
        cron.batch_gen, "generate_batch_for_all_users", return_value=[]
    ), patch.object(
        cron.send_worker, "drain_due_items",
        return_value=_FakeDrain(attempted=0, sent=0, failed=0, skipped=0, failures_by_kind={}),
    ), patch.object(
        cron.reply_ingestor, "ingest_replies_for_all_users", return_value=[]
    ):
        result = cron.run_cycle()

    assert result.batch_users_processed == 0
    assert result.batch_items_created == 0
    assert result.sent == 0
    assert result.replies_matched == 0
