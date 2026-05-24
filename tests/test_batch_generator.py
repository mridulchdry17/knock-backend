"""Tests for the B5.4 batch-generator orchestration service.

End-to-end with a real SQLite DB: seed users + companies + contacts + locks,
run `generate_batch_for_user`, assert TodayBatchItem rows + skip reasons.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from random import Random

import pytest
from sqlalchemy.orm import Session

from app.models import Company, Contact, User
from app.repositories import locks as locks_repo
from app.repositories import preferences as prefs_repo
from app.repositories import today_batch as today_repo
from app.services import batch_generator as batch_gen_svc
from tests.conftest import _make_user

BATCH_DATE = date(2026, 5, 12)


# ─────────────────────────── fixtures ───────────────────────────


def _make_company(db: Session, *, domain: str, name: str | None = None) -> Company:
    co = Company(domain=domain, name=name or domain, source="seed")
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_contact(
    db: Session,
    *,
    company: Company,
    email: str,
    name: str | None = None,
    is_invalid: bool = False,
) -> Contact:
    c = Contact(
        company_id=company.id,
        email=email,
        name=name,
        is_invalid=is_invalid,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _seed_n_companies_with_contacts(
    db: Session, *, n: int, contacts_per_company: int = 1
) -> list[Company]:
    out: list[Company] = []
    for i in range(n):
        domain = f"company{i}.com"
        co = _make_company(db, domain=domain, name=f"Company {i}")
        for j in range(contacts_per_company):
            _make_contact(db, company=co, email=f"u{i}_{j}@{domain}", name=f"User {i}{j}")
        out.append(co)
    return out


@pytest.fixture
def free_user(db: Session) -> User:
    user = _make_user(
        db,
        email="free@x.com",
        google_sub="g-free",
        tier="free",
        waitlist_email="free@x.com",
    )
    user.google_refresh_token = "fake-encrypted"
    user.daily_limit = 0  # use tier default
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def paid_user(db: Session) -> User:
    user = _make_user(
        db,
        email="paid@x.com",
        google_sub="g-paid",
        tier="paid",
        waitlist_email="paid@x.com",
    )
    user.google_refresh_token = "fake-encrypted"
    user.daily_limit = 0
    db.add(user)
    db.commit()
    return user


# ─────────────────────────── eligibility gates ───────────────────────────


def test_pending_user_skipped(db: Session) -> None:
    user = _make_user(db, email="p@x.com", google_sub="g-p", tier="pending")
    user.google_refresh_token = "fake"
    db.add(user)
    db.commit()
    _seed_n_companies_with_contacts(db, n=3)

    result = batch_gen_svc.generate_batch_for_user(
        db, user, batch_date=BATCH_DATE, rng=Random(1)
    )
    assert result.items_created == 0
    assert result.reason_if_skipped == "pending_tier"


def test_suspended_user_skipped(db: Session, free_user: User) -> None:
    free_user.is_suspended = True
    db.add(free_user)
    db.commit()
    _seed_n_companies_with_contacts(db, n=3)

    result = batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    assert result.reason_if_skipped == "suspended"


def test_gmail_disconnected_skipped(db: Session) -> None:
    user = _make_user(
        db,
        email="ng@x.com",
        google_sub="g-ng",
        tier="free",
        waitlist_email="ng@x.com",
    )
    # google_refresh_token left None
    db.add(user)
    db.commit()
    _seed_n_companies_with_contacts(db, n=3)

    result = batch_gen_svc.generate_batch_for_user(
        db, user, batch_date=BATCH_DATE, rng=Random(1)
    )
    assert result.reason_if_skipped == "gmail_disconnected"


# ─────────────────────────── tier → cap ───────────────────────────


def test_free_user_cap_is_seven(db: Session, free_user: User) -> None:
    _seed_n_companies_with_contacts(db, n=15)
    result = batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    assert result.items_created == 7


def test_paid_user_cap_is_twenty(db: Session, paid_user: User) -> None:
    _seed_n_companies_with_contacts(db, n=30)
    result = batch_gen_svc.generate_batch_for_user(
        db, paid_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    assert result.items_created == 20


def test_super_admin_treated_as_paid(db: Session) -> None:
    user = _make_user(
        db,
        email="admin@x.com",
        google_sub="g-admin",
        tier="super_admin",
        waitlist_email="admin@x.com",
    )
    user.google_refresh_token = "fake"
    user.daily_limit = 0
    db.add(user)
    db.commit()
    _seed_n_companies_with_contacts(db, n=30)

    result = batch_gen_svc.generate_batch_for_user(
        db, user, batch_date=BATCH_DATE, rng=Random(1)
    )
    assert result.items_created == 20


def test_daily_limit_override_beats_tier_default(db: Session, free_user: User) -> None:
    free_user.daily_limit = 3
    db.add(free_user)
    db.commit()
    _seed_n_companies_with_contacts(db, n=10)

    result = batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    assert result.items_created == 3


# ─────────────────────────── filters ───────────────────────────


def test_excluded_domain_not_picked(db: Session, free_user: User) -> None:
    companies = _seed_n_companies_with_contacts(db, n=2)
    prefs_repo.add_excluded_domain(db, free_user.id, companies[0].domain)
    db.commit()

    result = batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    assert result.items_created == 1
    items = today_repo.list_for_user_date(db, free_user.id, BATCH_DATE)
    assert all(i.company_domain != companies[0].domain for i in items)


def test_user_reply_lock_blocks_company(db: Session, free_user: User) -> None:
    companies = _seed_n_companies_with_contacts(db, n=2)
    locks_repo.upsert_user_company_lock(
        db, free_user.id, companies[0].domain, reason="reply"
    )
    db.commit()

    result = batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    assert result.items_created == 1
    items = today_repo.list_for_user_date(db, free_user.id, BATCH_DATE)
    assert items[0].company_domain == companies[1].domain


def test_platform_permanent_lock_blocks_company(db: Session, free_user: User) -> None:
    companies = _seed_n_companies_with_contacts(db, n=2)
    locks_repo.upsert_platform_lock(db, companies[0].domain, reason="explicit_stop_reply")
    db.commit()

    result = batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    assert result.items_created == 1


def test_global_cooldown_blocks_company(db: Session, free_user: User) -> None:
    companies = _seed_n_companies_with_contacts(db, n=2)
    # A platform-wide cooldown exists on companies[0]. Attribution is nullable
    # (SET NULL) and the picker keys off company_domain + locked_until, not who
    # set it, so leave last_locked_by_user_id unset rather than referencing a
    # non-existent user id (which a real FK now correctly rejects).
    locks_repo.upsert_global_lock(
        db, companies[0].domain, locked_by_user_id=None, lock_duration_hours=36
    )
    db.commit()

    result = batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    assert result.items_created == 1
    items = today_repo.list_for_user_date(db, free_user.id, BATCH_DATE)
    assert items[0].company_domain == companies[1].domain


# ─────────────────────────── status (autopilot vs manual) ───────────────────────────


def test_autopilot_user_items_status_ready(db: Session, paid_user: User) -> None:
    paid_user.autopilot_enabled = True
    paid_user.autopilot_paused_at = None
    db.add(paid_user)
    db.commit()
    _seed_n_companies_with_contacts(db, n=3)

    batch_gen_svc.generate_batch_for_user(
        db, paid_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    items = today_repo.list_for_user_date(db, paid_user.id, BATCH_DATE)
    assert items and all(i.status == "ready" for i in items)


def test_manual_user_items_status_default(db: Session, free_user: User) -> None:
    _seed_n_companies_with_contacts(db, n=3)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    items = today_repo.list_for_user_date(db, free_user.id, BATCH_DATE)
    assert items and all(i.status == "default" for i in items)


def test_paused_autopilot_falls_back_to_default(db: Session, paid_user: User) -> None:
    paid_user.autopilot_enabled = True
    paid_user.autopilot_paused_at = datetime.now(UTC)
    db.add(paid_user)
    db.commit()
    _seed_n_companies_with_contacts(db, n=3)

    batch_gen_svc.generate_batch_for_user(
        db, paid_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    items = today_repo.list_for_user_date(db, paid_user.id, BATCH_DATE)
    assert items and all(i.status == "default" for i in items)


# ─────────────────────────── idempotency ───────────────────────────


def test_second_run_same_day_is_noop(db: Session, free_user: User) -> None:
    _seed_n_companies_with_contacts(db, n=5)
    first = batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    assert first.items_created == 5

    second = batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    assert second.items_created == 0
    assert second.reason_if_skipped == "already_run_today"
    # No duplicate rows.
    assert (
        len(today_repo.list_for_user_date(db, free_user.id, BATCH_DATE)) == 5
    )


def test_different_dates_dont_conflict(db: Session, free_user: User) -> None:
    _seed_n_companies_with_contacts(db, n=5)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    next_day = BATCH_DATE + timedelta(days=1)
    second = batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=next_day, rng=Random(2)
    )
    assert second.items_created == 5


# ─────────────────────────── sent_today reset ───────────────────────────


def test_sent_today_resets_on_new_day(db: Session, free_user: User) -> None:
    free_user.sent_today = 5
    free_user.last_reset_date = BATCH_DATE - timedelta(days=1)
    db.add(free_user)
    db.commit()
    _seed_n_companies_with_contacts(db, n=3)

    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    db.refresh(free_user)
    assert free_user.sent_today == 0
    assert free_user.last_reset_date == BATCH_DATE


def test_sent_today_not_reset_same_day(db: Session, free_user: User) -> None:
    free_user.sent_today = 3
    free_user.last_reset_date = BATCH_DATE
    db.add(free_user)
    db.commit()
    _seed_n_companies_with_contacts(db, n=3)

    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    db.refresh(free_user)
    assert free_user.sent_today == 3


# ─────────────────────────── per-company sampling ───────────────────────────


def test_sampling_caps_recipients_at_five(db: Session, free_user: User) -> None:
    co = _make_company(db, domain="big.com", name="Big Co")
    for i in range(10):
        _make_contact(db, company=co, email=f"u{i}@big.com", name=f"U{i}")

    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    items = today_repo.list_for_user_date(db, free_user.id, BATCH_DATE)
    assert len(items) == 1
    item = items[0]
    cc_ids = item.get_cc_contact_ids()
    assert len(cc_ids) == 4
    assert item.to_contact_id not in cc_ids


# ─────────────────────────── empty pool ───────────────────────────


def test_no_candidates_returns_no_eligible_contacts(
    db: Session, free_user: User
) -> None:
    # No companies seeded.
    result = batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    assert result.items_created == 0
    assert result.reason_if_skipped == "no_eligible_contacts"


def test_invalid_contacts_skipped(db: Session, free_user: User) -> None:
    co = _make_company(db, domain="ghost.com")
    _make_contact(db, company=co, email="ghost@ghost.com", is_invalid=True)
    result = batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    assert result.items_created == 0
    assert result.reason_if_skipped == "no_eligible_contacts"


# ─────────────────────────── generate_batch_for_all_users ───────────────────────────


def test_generate_for_all_users_iterates(db: Session) -> None:
    free = _make_user(
        db, email="f@x.com", google_sub="g-f", tier="free", waitlist_email="f@x.com"
    )
    free.google_refresh_token = "fake"
    paid = _make_user(
        db, email="p@x.com", google_sub="g-p", tier="paid", waitlist_email="p@x.com"
    )
    paid.google_refresh_token = "fake"
    pending = _make_user(db, email="pn@x.com", google_sub="g-pn", tier="pending")
    db.add_all([free, paid, pending])
    db.commit()

    _seed_n_companies_with_contacts(db, n=5)

    results = batch_gen_svc.generate_batch_for_all_users(
        db, batch_date=BATCH_DATE, rng=Random(7)
    )
    assert len(results) == 3
    by_user = {r.user_id: r for r in results}
    assert by_user[free.id].items_created == 5
    assert by_user[paid.id].items_created == 5
    assert by_user[pending.id].reason_if_skipped == "pending_tier"


# ─────────────────────────── template rendering ───────────────────────────


def test_default_template_renders_first_name_and_company(
    db: Session, free_user: User
) -> None:
    co = _make_company(db, domain="acme.com", name="Acme Inc")
    _make_contact(db, company=co, email="jane@acme.com", name="Jane Doe")

    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=BATCH_DATE, rng=Random(1)
    )
    items = today_repo.list_for_user_date(db, free_user.id, BATCH_DATE)
    assert len(items) == 1
    assert "Jane" in items[0].body
    assert "Acme Inc" in items[0].body
    assert "{{first_name}}" not in items[0].body
    assert "{{company}}" not in items[0].body
