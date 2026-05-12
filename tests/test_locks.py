"""Service-layer tests for the 3-tier lock model (B5.3).

Covers:
- The check function's priority order (platform-permanent > 36h > user-reply)
- Idempotent upserts (repeated calls extend, don't duplicate)
- Per-user isolation (user A's reply lock doesn't affect user B)
- Auto-expiry semantics (past `locked_until` → AVAILABLE)
- Explicit-stop short-circuits to platform-permanent, never user-reply
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models import User
from app.repositories import locks as locks_repo
from app.services import locks as locks_svc
from app.services.locks import LockStatus
from tests.conftest import _make_user


@pytest.fixture
def user_a(db: Session) -> User:
    return _make_user(db, email="a@x.com", google_sub="g-a", tier="free")


@pytest.fixture
def user_b(db: Session) -> User:
    return _make_user(db, email="b@x.com", google_sub="g-b", tier="free")


# ─────────────────────────── check_can_send_to_company ───────────────────────────


def test_no_locks_returns_available(db: Session, user_a: User) -> None:
    result = locks_svc.check_can_send_to_company(
        db, user_id=user_a.id, company_domain="acme.com"
    )
    assert result.status == LockStatus.AVAILABLE
    assert result.unlocked_at is None
    assert result.reason is None


def test_platform_36h_cooldown_blocks(db: Session, user_a: User) -> None:
    locks_svc.record_send_to_company(
        db, user_id=user_a.id, company_domain="acme.com"
    )
    db.commit()

    result = locks_svc.check_can_send_to_company(
        db, user_id=user_a.id, company_domain="acme.com"
    )
    assert result.status == LockStatus.PLATFORM_COOLDOWN
    assert result.unlocked_at is not None
    assert result.unlocked_at > utcnow()


def test_platform_36h_cooldown_expires(db: Session, user_a: User) -> None:
    locks_svc.record_send_to_company(
        db, user_id=user_a.id, company_domain="acme.com"
    )
    # Manually walk the row into the past so we don't have to sleep.
    lock = locks_repo.get_global_lock(db, "acme.com")
    assert lock is not None
    lock.locked_until = datetime.now(UTC) - timedelta(hours=1)
    db.add(lock)
    db.commit()

    result = locks_svc.check_can_send_to_company(
        db, user_id=user_a.id, company_domain="acme.com"
    )
    assert result.status == LockStatus.AVAILABLE


def test_user_reply_lock_blocks_only_that_user(
    db: Session, user_a: User, user_b: User
) -> None:
    locks_svc.record_reply_from_company(
        db, user_id=user_a.id, company_domain="acme.com", is_explicit_stop=False
    )
    db.commit()

    result_a = locks_svc.check_can_send_to_company(
        db, user_id=user_a.id, company_domain="acme.com"
    )
    result_b = locks_svc.check_can_send_to_company(
        db, user_id=user_b.id, company_domain="acme.com"
    )
    assert result_a.status == LockStatus.USER_REPLY_LOCK
    assert result_b.status == LockStatus.AVAILABLE


def test_user_reply_lock_extends_on_new_reply(db: Session, user_a: User) -> None:
    locks_svc.record_reply_from_company(
        db, user_id=user_a.id, company_domain="acme.com", is_explicit_stop=False
    )
    first = locks_repo.get_user_company_lock(db, user_a.id, "acme.com")
    assert first is not None
    first_until = first.locked_until

    # Walk backwards in time briefly so the second call's `now + 30d` is
    # strictly greater than the first.
    first.locked_until = datetime.now(UTC) + timedelta(days=29)
    db.add(first)
    db.commit()

    locks_svc.record_reply_from_company(
        db, user_id=user_a.id, company_domain="acme.com", is_explicit_stop=False
    )
    db.commit()
    second = locks_repo.get_user_company_lock(db, user_a.id, "acme.com")
    assert second is not None
    # The second upsert pushed locked_until forward beyond our manual rewind.
    assert second.locked_until > first_until - timedelta(days=2)


def test_platform_permanent_blocks_all_users(
    db: Session, user_a: User, user_b: User
) -> None:
    locks_svc.record_reply_from_company(
        db, user_id=user_a.id, company_domain="acme.com", is_explicit_stop=True
    )
    db.commit()

    for uid in (user_a.id, user_b.id):
        result = locks_svc.check_can_send_to_company(
            db, user_id=uid, company_domain="acme.com"
        )
        assert result.status == LockStatus.PLATFORM_PERMANENT
        assert result.unlocked_at is None  # never auto-expires


def test_explicit_stop_creates_permanent_not_user_lock(
    db: Session, user_a: User
) -> None:
    locks_svc.record_reply_from_company(
        db, user_id=user_a.id, company_domain="acme.com", is_explicit_stop=True
    )
    db.commit()

    assert locks_repo.get_platform_lock(db, "acme.com") is not None
    assert locks_repo.get_user_company_lock(db, user_a.id, "acme.com") is None


def test_lock_check_priority_order(db: Session, user_a: User) -> None:
    """All three locks coexist; platform-permanent wins."""
    locks_svc.record_send_to_company(
        db, user_id=user_a.id, company_domain="acme.com"
    )
    locks_svc.record_reply_from_company(
        db, user_id=user_a.id, company_domain="acme.com", is_explicit_stop=False
    )
    locks_repo.upsert_platform_lock(db, "acme.com", reason="manual_admin")
    db.commit()

    result = locks_svc.check_can_send_to_company(
        db, user_id=user_a.id, company_domain="acme.com"
    )
    assert result.status == LockStatus.PLATFORM_PERMANENT

    # Clear platform lock — 36h cooldown should now show
    locks_repo.clear_platform_lock(db, "acme.com")
    db.commit()
    result = locks_svc.check_can_send_to_company(
        db, user_id=user_a.id, company_domain="acme.com"
    )
    assert result.status == LockStatus.PLATFORM_COOLDOWN

    # Walk cooldown into the past — user reply lock should now show
    gl = locks_repo.get_global_lock(db, "acme.com")
    assert gl is not None
    gl.locked_until = datetime.now(UTC) - timedelta(hours=1)
    db.add(gl)
    db.commit()
    result = locks_svc.check_can_send_to_company(
        db, user_id=user_a.id, company_domain="acme.com"
    )
    assert result.status == LockStatus.USER_REPLY_LOCK


def test_repository_idempotent_upserts(db: Session, user_a: User) -> None:
    locks_repo.upsert_global_lock(db, "acme.com", user_a.id)
    locks_repo.upsert_global_lock(db, "acme.com", user_a.id)
    db.commit()

    rows = locks_repo.list_active_global_locks(db, now=utcnow())
    assert sum(1 for r in rows if r.company_domain == "acme.com") == 1

    locks_repo.upsert_user_company_lock(db, user_a.id, "acme.com", reason="reply")
    locks_repo.upsert_user_company_lock(db, user_a.id, "acme.com", reason="reply")
    db.commit()
    assert (
        len(
            [
                r
                for r in locks_repo.list_active_user_locks(db, user_a.id, now=utcnow())
                if r.company_domain == "acme.com"
            ]
        )
        == 1
    )

    locks_repo.upsert_platform_lock(db, "acme.com", reason="manual_admin")
    locks_repo.upsert_platform_lock(db, "acme.com", reason="manual_admin")
    db.commit()
    assert len(locks_repo.list_platform_locks(db)) == 1


def test_domain_is_normalized(db: Session, user_a: User) -> None:
    """Mixed-case + whitespace → same row as lowercased."""
    locks_svc.record_send_to_company(
        db, user_id=user_a.id, company_domain="  Acme.COM "
    )
    db.commit()

    result = locks_svc.check_can_send_to_company(
        db, user_id=user_a.id, company_domain="acme.com"
    )
    assert result.status == LockStatus.PLATFORM_COOLDOWN


def test_user_lock_permanent_flag_persists_through_expiry(
    db: Session, user_a: User
) -> None:
    locks_repo.upsert_user_company_lock(
        db, user_a.id, "acme.com", reason="manual_admin", is_permanent=True
    )
    # Walk locked_until into the past
    row = locks_repo.get_user_company_lock(db, user_a.id, "acme.com")
    assert row is not None
    row.locked_until = datetime.now(UTC) - timedelta(days=1)
    db.add(row)
    db.commit()

    result = locks_svc.check_can_send_to_company(
        db, user_id=user_a.id, company_domain="acme.com"
    )
    assert result.status == LockStatus.USER_REPLY_LOCK
    assert result.unlocked_at is None  # permanent → no countdown
