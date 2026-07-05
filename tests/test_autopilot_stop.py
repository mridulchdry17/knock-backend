"""Tests for app.services.autopilot_stop.

Covers per-condition triggering, platform ceilings, the ceiling-vs-user
priority ordering, and the NULL-enabled_at safety guard.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models import Contact, SendQueue, User
from app.services import autopilot_stop
from tests.conftest import _make_user


# ─────────────────────────── fixtures ───────────────────────────


@pytest.fixture
def autopilot_user(db: Session) -> User:
    """A paid user with autopilot toggled on ~30 days ago. Fresh state:
    no stop condition, no paused_at, no reason."""
    user = _make_user(
        db,
        email="ap@example.com",
        google_sub="g-ap",
        tier="paid",
        waitlist_email="ap@example.com",
    )
    user.autopilot_enabled = True
    user.autopilot_enabled_at = utcnow() - timedelta(days=30)
    db.add(user)
    db.commit()
    return user


def _seed_contact(db: Session, *, email: str = "c@x.com") -> Contact:
    """SendQueue needs a real contact_id (FK NOT NULL). We don't care about
    contact fields — just need the FK to satisfy."""
    from app.models import Company

    company = Company(domain="x.com", name="X", source="test")
    db.add(company)
    db.flush()
    contact = Contact(company_id=company.id, name="Foo", email=email, role="e")
    db.add(contact)
    db.flush()
    return contact


def _seed_sends(
    db: Session, user: User, contact: Contact, *, count: int, sent_at: datetime
) -> None:
    """Seed N status='SENT' send_queue rows for `user`, all with sent_at=sent_at."""
    for i in range(count):
        row = SendQueue(
            user_id=user.id,
            contact_id=contact.id,
            to_contact_id=contact.id,
            cc_contact_ids="[]",
            company_domain="x.com",
            subject=f"s{i}",
            body_text="b",
            kind="INITIAL",
            scheduled_for=sent_at,
            status="SENT",
            sent_at=sent_at,
        )
        db.add(row)
    db.commit()


def _seed_replies(
    db: Session,
    user: User,
    contact: Contact,
    *,
    count: int,
    replied_at: datetime,
) -> None:
    """Seed N status='REPLIED' rows for `user`."""
    for i in range(count):
        row = SendQueue(
            user_id=user.id,
            contact_id=contact.id,
            to_contact_id=contact.id,
            cc_contact_ids="[]",
            company_domain="x.com",
            subject=f"r{i}",
            body_text="b",
            kind="INITIAL",
            scheduled_for=replied_at,
            status="REPLIED",
            sent_at=replied_at - timedelta(hours=1),
            replied_at=replied_at,
        )
        db.add(row)
    db.commit()


# ─────────────────────────── happy path ───────────────────────────


def test_should_pause_none_with_no_ceilings(autopilot_user, db) -> None:
    """Default state: stop_type='none', no sends, no replies → (False, None)."""
    pause, reason = autopilot_stop.should_pause(autopilot_user, db)
    assert (pause, reason) == (False, None)


def test_should_pause_none_with_null_enabled_at(db) -> None:
    """A freshly-migrated user who wasn't autopilot before: enabled_at=NULL.
    Never crashes; counters treated as zero."""
    user = _make_user(
        db,
        email="new@example.com",
        google_sub="g-new",
        tier="paid",
        waitlist_email="new@example.com",
    )
    user.autopilot_enabled = True
    user.autopilot_enabled_at = None  # explicit
    user.autopilot_stop_type = "replies"
    user.autopilot_stop_at_replies = 1
    db.add(user)
    db.commit()

    pause, reason = autopilot_stop.should_pause(user, db)
    assert (pause, reason) == (False, None)


# ─────────────────────────── user conditions ───────────────────────────


def test_replies_condition_triggers_at_threshold(autopilot_user, db) -> None:
    autopilot_user.autopilot_stop_type = "replies"
    autopilot_user.autopilot_stop_at_replies = 3
    db.add(autopilot_user)
    db.commit()

    contact = _seed_contact(db)
    _seed_replies(db, autopilot_user, contact, count=2, replied_at=utcnow())
    pause, _ = autopilot_stop.should_pause(autopilot_user, db)
    assert pause is False, "2 replies should not trip a threshold of 3"

    _seed_replies(db, autopilot_user, contact, count=1, replied_at=utcnow())
    pause, reason = autopilot_stop.should_pause(autopilot_user, db)
    assert pause is True
    assert reason == "replies"


def test_replies_condition_ignores_pre_enabled_replies(autopilot_user, db) -> None:
    """Replies that landed BEFORE autopilot_enabled_at shouldn't count."""
    autopilot_user.autopilot_stop_type = "replies"
    autopilot_user.autopilot_stop_at_replies = 1
    db.add(autopilot_user)
    db.commit()

    contact = _seed_contact(db)
    # Reply from before the anchor: 60 days ago, anchor was 30 days ago.
    _seed_replies(
        db, autopilot_user, contact, count=5, replied_at=utcnow() - timedelta(days=60)
    )
    pause, _ = autopilot_stop.should_pause(autopilot_user, db)
    assert pause is False


def test_end_date_condition_fires_on_or_after_target(autopilot_user, db) -> None:
    yesterday = utcnow().date() - timedelta(days=1)
    tomorrow = utcnow().date() + timedelta(days=1)

    autopilot_user.autopilot_stop_type = "end_date"
    autopilot_user.autopilot_stop_at_date = tomorrow
    db.add(autopilot_user)
    db.commit()

    pause, _ = autopilot_stop.should_pause(autopilot_user, db)
    assert pause is False, "tomorrow shouldn't fire today"

    autopilot_user.autopilot_stop_at_date = yesterday
    db.add(autopilot_user)
    db.commit()

    pause, reason = autopilot_stop.should_pause(autopilot_user, db)
    assert pause is True
    assert reason == "end_date"


def test_end_date_fires_on_same_day(autopilot_user, db) -> None:
    """Same-day trigger — spec says `today() >= target`."""
    autopilot_user.autopilot_stop_type = "end_date"
    autopilot_user.autopilot_stop_at_date = utcnow().date()
    db.add(autopilot_user)
    db.commit()

    pause, reason = autopilot_stop.should_pause(autopilot_user, db)
    assert (pause, reason) == (True, "end_date")


def test_budget_condition_triggers_at_threshold(autopilot_user, db) -> None:
    autopilot_user.autopilot_stop_type = "budget"
    autopilot_user.autopilot_stop_at_budget = 50
    db.add(autopilot_user)
    db.commit()

    contact = _seed_contact(db)
    _seed_sends(db, autopilot_user, contact, count=49, sent_at=utcnow())
    pause, _ = autopilot_stop.should_pause(autopilot_user, db)
    assert pause is False, "49 sends should not trip a budget of 50"

    _seed_sends(db, autopilot_user, contact, count=1, sent_at=utcnow())
    pause, reason = autopilot_stop.should_pause(autopilot_user, db)
    assert pause is True
    assert reason == "budget"


# ─────────────────────────── platform ceilings ───────────────────────────


def test_ceiling_sends_triggers_at_500(autopilot_user, db) -> None:
    """500 sends since enabled → 'ceiling_sends' regardless of stop_type."""
    autopilot_user.autopilot_stop_type = "none"
    db.add(autopilot_user)
    db.commit()

    contact = _seed_contact(db)
    _seed_sends(
        db, autopilot_user, contact, count=autopilot_stop.CEILING_MAX_SENDS, sent_at=utcnow()
    )

    pause, reason = autopilot_stop.should_pause(autopilot_user, db)
    assert pause is True
    assert reason == "ceiling_sends"


def test_ceiling_days_triggers_at_90(autopilot_user, db) -> None:
    """90+ days since enabled → 'ceiling_days'. Simulate by moving enabled_at
    into the deep past."""
    autopilot_user.autopilot_enabled_at = utcnow() - timedelta(days=91)
    autopilot_user.autopilot_stop_type = "none"
    db.add(autopilot_user)
    db.commit()

    pause, reason = autopilot_stop.should_pause(autopilot_user, db)
    assert pause is True
    assert reason == "ceiling_days"


def test_ceiling_wins_over_user_condition(autopilot_user, db) -> None:
    """When BOTH the platform ceiling and the user's condition would fire,
    the ceiling reason is returned. This is what distinguishes system-
    imposed pauses from user-configured ones."""
    # Set both: user picked 5 replies, but they've hit the 500-send ceiling.
    autopilot_user.autopilot_stop_type = "replies"
    autopilot_user.autopilot_stop_at_replies = 5
    db.add(autopilot_user)
    db.commit()

    contact = _seed_contact(db)
    _seed_sends(
        db, autopilot_user, contact, count=autopilot_stop.CEILING_MAX_SENDS, sent_at=utcnow()
    )
    _seed_replies(db, autopilot_user, contact, count=5, replied_at=utcnow())

    pause, reason = autopilot_stop.should_pause(autopilot_user, db)
    assert pause is True
    assert reason == "ceiling_sends", (
        "ceiling should win over user's replies condition"
    )


def test_ceiling_days_wins_over_end_date(autopilot_user, db) -> None:
    """Similar priority check for the days ceiling."""
    autopilot_user.autopilot_enabled_at = utcnow() - timedelta(days=100)
    autopilot_user.autopilot_stop_type = "end_date"
    # Set an end date in the future — user's condition alone wouldn't fire.
    autopilot_user.autopilot_stop_at_date = utcnow().date() + timedelta(days=5)
    db.add(autopilot_user)
    db.commit()

    pause, reason = autopilot_stop.should_pause(autopilot_user, db)
    assert pause is True
    assert reason == "ceiling_days"


# ─────────────────────────── counters honor anchor ───────────────────────────


def test_sends_counter_ignores_pre_anchor_rows(autopilot_user, db) -> None:
    """A send that landed BEFORE autopilot_enabled_at doesn't count toward
    either the budget condition or the sends ceiling."""
    autopilot_user.autopilot_stop_type = "budget"
    autopilot_user.autopilot_stop_at_budget = 25
    db.add(autopilot_user)
    db.commit()

    contact = _seed_contact(db)
    # 100 sends from 90 days ago — well before the 30-days-ago anchor.
    _seed_sends(
        db,
        autopilot_user,
        contact,
        count=100,
        sent_at=utcnow() - timedelta(days=90),
    )
    pause, _ = autopilot_stop.should_pause(autopilot_user, db)
    assert pause is False, "pre-anchor sends must not count"


def test_days_counter_returns_zero_when_enabled_today(autopilot_user, db) -> None:
    """Anchor = now → days-since = 0 → ceiling far from firing."""
    autopilot_user.autopilot_enabled_at = utcnow()
    db.add(autopilot_user)
    db.commit()

    pause, reason = autopilot_stop.should_pause(autopilot_user, db)
    assert (pause, reason) == (False, None)
