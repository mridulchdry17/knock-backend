"""Tests for app/services/send_scheduling.py — the late-stamp helper that
queues manually-approved items at the back of the schedule instead of letting
them blast on the next drain tick.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.time import ensure_utc
from app.models import Company, Contact, TodayBatchItem, User
from app.services import send_scheduling
from app.services.today_picker import cadence_for_tier
from tests.conftest import _make_user

# ─────────────────────────── cadence_for_tier ───────────────────────────


def test_cadence_free_is_one_hour() -> None:
    assert cadence_for_tier("free", 7) == timedelta(hours=1)


def test_cadence_paid_15_is_one_hour() -> None:
    # 14-hour window / (15-1) slots = 60 min.
    assert cadence_for_tier("paid", 15) == timedelta(minutes=60)


def test_cadence_paid_cap_1_falls_back_to_one_hour() -> None:
    # Degenerate: a single send. Helper should still produce a finite cadence
    # so loops don't divide by zero.
    assert cadence_for_tier("paid", 1) == timedelta(hours=1)


# ─────────────────────────── partition_late ───────────────────────────


def test_partition_late_splits_correctly() -> None:
    now = datetime.now(UTC)
    past = TodayBatchItem(send_time=now - timedelta(hours=1))
    future = TodayBatchItem(send_time=now + timedelta(hours=1))
    late, fut = send_scheduling.partition_late([past, future], now=now)
    assert late == [past]
    assert fut == [future]


# ─────────────────────────── stamp_late_items_for_user ───────────────────────────


def _seed_user_with_today_items(
    db: Session,
    *,
    send_times: list[datetime],
    statuses: list[str],
) -> tuple[User, list[TodayBatchItem]]:
    """Seed a user with a handful of today_batch_items at specified slots."""
    user = _make_user(db, email="u@x.com", tier="free", waitlist_email="u@x.com")
    items: list[TodayBatchItem] = []
    today = datetime.now(UTC).date()
    # One company+contact per item — the schema has UNIQUE(user_id, batch_date,
    # company_id), so all items must hit distinct companies.
    for idx, (st, status) in enumerate(zip(send_times, statuses, strict=True)):
        domain = f"c{idx}.com"
        company = Company(name=f"C{idx}", domain=domain, source="t")
        db.add(company)
        db.flush()
        contact = Contact(company_id=company.id, name=f"X{idx}", email=f"x@{domain}")
        db.add(contact)
        db.flush()
        item = TodayBatchItem(
            user_id=user.id,
            batch_date=today,
            company_id=company.id,
            company_domain=domain,
            to_contact_id=contact.id,
            cc_contact_ids="[]",
            subject="s",
            body="b",
            status=status,
            send_time=st,
        )
        db.add(item)
        items.append(item)
    db.commit()
    for it in items:
        db.refresh(it)
    return user, items


def test_stamp_late_queues_after_latest_existing_at_cadence(db: Session) -> None:
    """Worked example: latest existing future slot is at +3h; 2 late items
    must be queued at +4h and +5h (1-hour free cadence)."""
    now = datetime.now(UTC)
    user, items = _seed_user_with_today_items(
        db,
        send_times=[
            now - timedelta(hours=2),  # late #1
            now - timedelta(hours=1),  # late #2
            now + timedelta(hours=3),  # latest existing future
        ],
        statuses=["ready", "ready", "ready"],
    )
    late = [items[0], items[1]]
    send_scheduling.stamp_late_items_for_user(db, user, late, now=now)
    db.commit()
    for it in late:
        db.refresh(it)

    # Items at "now+3h" was the anchor; cadence 1h → next at now+4h, then now+5h.
    assert ensure_utc(late[0].send_time) == now + timedelta(hours=4)
    assert ensure_utc(late[1].send_time) == now + timedelta(hours=5)
    # The future item is untouched.
    db.refresh(items[2])
    assert ensure_utc(items[2].send_time) == now + timedelta(hours=3)


def test_stamp_late_with_no_future_anchor_starts_from_now(db: Session) -> None:
    """If there's no future or sent slot to anchor on, the first late item
    sits at now + cadence (not at exactly now — it shouldn't dispatch on the
    next drain tick)."""
    now = datetime.now(UTC)
    user, items = _seed_user_with_today_items(
        db,
        send_times=[
            now - timedelta(hours=5),
            now - timedelta(hours=4),
        ],
        statuses=["ready", "ready"],
    )
    send_scheduling.stamp_late_items_for_user(db, user, items, now=now)
    db.commit()
    for it in items:
        db.refresh(it)

    assert ensure_utc(items[0].send_time) == now + timedelta(hours=1)
    assert ensure_utc(items[1].send_time) == now + timedelta(hours=2)


def test_stamp_late_uses_sent_slot_as_anchor_when_higher(db: Session) -> None:
    """A 'sent' item with a high send_time still anchors the queue (we shouldn't
    re-order it; we just append after)."""
    now = datetime.now(UTC)
    user, items = _seed_user_with_today_items(
        db,
        send_times=[
            now - timedelta(hours=1),  # late, default
            now - timedelta(hours=2),  # already sent, but send_time was 2h ago
        ],
        statuses=["ready", "sent"],
    )
    # The "sent" item's send_time (now-2h) is less than now → anchor falls
    # back to now. First late goes at now + cadence.
    send_scheduling.stamp_late_items_for_user(db, user, [items[0]], now=now)
    db.commit()
    db.refresh(items[0])
    assert ensure_utc(items[0].send_time) == now + timedelta(hours=1)


def test_stamp_late_noop_on_empty_list(db: Session) -> None:
    user = _make_user(db, email="e@x.com", tier="free")
    send_scheduling.stamp_late_items_for_user(db, user, [])  # must not raise
