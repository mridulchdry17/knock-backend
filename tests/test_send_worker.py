"""Tests for the B5.5 send worker (`app.services.send_worker.drain_due_items`).

Strategy: replace `gmail_send.send_email` with a stub that returns
configurable SendResults. This isolates the worker logic from the Gmail SDK
entirely — we verify state transitions, commits, lock writes, send_queue
inserts, gmail_disconnected flips, and skip-paths.

We also bypass `get_user_credentials` because it depends on a real Fernet
TOKEN_ENCRYPTION_KEY + decrypted tokens; that's covered separately in the
adapter tests.
"""
from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models import (
    Company,
    Contact,
    EmailFailure,
    GlobalContactLock,
    SendQueue,
    TodayBatchItem,
    User,
)
from app.repositories import locks as locks_repo
from app.services import gmail_send, send_worker
from tests.conftest import _make_user

# ─────────────────────────── fixtures ───────────────────────────


@pytest.fixture
def user(db: Session) -> User:
    u = _make_user(db, email="alice@example.com", google_sub="g-a", tier="free")
    # Stub a refresh token so get_user_credentials wouldn't raise (we patch it
    # away anyway, but having something here is closer to production state).
    u.google_refresh_token = "encrypted-refresh"
    u.google_access_token = "encrypted-access"
    u.full_name = "Alice"
    db.commit()
    return u


@pytest.fixture
def company(db: Session) -> Company:
    c = Company(domain="acme.com", name="Acme Inc", source="seed")
    db.add(c)
    db.commit()
    return c


@pytest.fixture
def to_contact(db: Session, company: Company) -> Contact:
    c = Contact(company_id=company.id, email="bob@acme.com", name="Bob")
    db.add(c)
    db.commit()
    return c


@pytest.fixture
def cc_contact(db: Session, company: Company) -> Contact:
    c = Contact(company_id=company.id, email="carol@acme.com", name="Carol")
    db.add(c)
    db.commit()
    return c


def _make_item(
    db: Session,
    *,
    user: User,
    company: Company,
    to_contact: Contact,
    cc_ids: list[int] | None = None,
    status: str = "ready",
    send_time_offset_seconds: int = -60,
) -> TodayBatchItem:
    """Helper to seed a today_batch_item. Defaults to a 1-minute-overdue 'ready' row."""
    item = TodayBatchItem(
        user_id=user.id,
        batch_date=utcnow().date(),
        company_id=company.id,
        company_domain=company.domain,
        to_contact_id=to_contact.id,
        cc_contact_ids=json.dumps(cc_ids or []),
        subject="Hello from Knock",
        body="Body text.",
        status=status,
        send_time=utcnow() + timedelta(seconds=send_time_offset_seconds),
    )
    db.add(item)
    db.commit()
    return item


def _stub_send(result: gmail_send.SendResult):
    """Returns a context manager that patches gmail_send.send_email + creds."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch.object(gmail_send, "send_email", return_value=result))
    stack.enter_context(
        patch.object(send_worker, "get_user_credentials", return_value=object())
    )
    return stack


# ─────────────────────────── happy path ───────────────────────────


def test_drain_happy_path_marks_sent_and_writes_audit(
    db: Session, user: User, company: Company, to_contact: Contact
) -> None:
    item = _make_item(db, user=user, company=company, to_contact=to_contact)
    success = gmail_send.SendResult(
        ok=True, gmail_message_id="msg-1", gmail_thread_id="thr-1"
    )

    with _stub_send(success):
        summary = send_worker.drain_due_items(db)

    assert summary.attempted == 1
    assert summary.sent == 1
    assert summary.failed == 0
    assert summary.skipped == 0

    db.expire_all()
    item = db.get(TodayBatchItem, item.id)
    assert item.status == "sent"
    assert item.sent_at is not None
    assert item.gmail_message_id == "msg-1"
    assert item.gmail_thread_id == "thr-1"

    # user.sent_today bumped
    u = db.get(User, user.id)
    assert u.sent_today == 1

    # send_queue row inserted
    sq = db.scalar(select(SendQueue).where(SendQueue.today_batch_item_id == item.id))
    assert sq is not None
    assert sq.gmail_message_id == "msg-1"
    assert sq.to_contact_id == to_contact.id
    assert sq.contact_id == to_contact.id  # legacy column mirrored
    assert sq.subject == "Hello from Knock"
    assert sq.company_domain == "acme.com"

    # 36h platform lock written
    lock = db.get(GlobalContactLock, "acme.com")
    assert lock is not None
    assert lock.last_locked_by_user_id == user.id

    # no email_failures row
    failures = db.scalars(select(EmailFailure)).all()
    assert list(failures) == []


def test_drain_passes_cc_emails_to_adapter(
    db: Session,
    user: User,
    company: Company,
    to_contact: Contact,
    cc_contact: Contact,
) -> None:
    _make_item(
        db, user=user, company=company, to_contact=to_contact, cc_ids=[cc_contact.id]
    )
    success = gmail_send.SendResult(ok=True, gmail_message_id="m")
    with patch.object(gmail_send, "send_email", return_value=success) as sent, patch.object(
        send_worker, "get_user_credentials", return_value=object()
    ):
        send_worker.drain_due_items(db)

    sent.assert_called_once()
    kwargs = sent.call_args.kwargs
    assert kwargs["to_email"] == "bob@acme.com"
    assert kwargs["cc_emails"] == ["carol@acme.com"]
    assert kwargs["sender_email"] == "alice@example.com"
    assert kwargs["sender_name"] == "Alice"


# ─────────────────────────── failure branches ───────────────────────────


@pytest.mark.parametrize(
    "kind", ["quota_exceeded", "recipient_rejected", "transient", "unknown"]
)
def test_drain_failure_marks_failed_and_inserts_email_failures(
    db: Session, user: User, company: Company, to_contact: Contact, kind: str
) -> None:
    item = _make_item(db, user=user, company=company, to_contact=to_contact)
    failure = gmail_send.SendResult(
        ok=False, failure_kind=kind, gmail_error_code="x", error_message="boom"
    )

    with _stub_send(failure):
        summary = send_worker.drain_due_items(db)

    assert summary.sent == 0 and summary.failed == 1
    assert summary.failures_by_kind == {kind: 1}

    db.expire_all()
    item = db.get(TodayBatchItem, item.id)
    assert item.status == "failed"

    row = db.scalar(select(EmailFailure))
    assert row is not None
    assert row.failure_kind == kind
    assert row.error_message == "boom"
    assert row.company_domain == "acme.com"

    # gmail_disconnected NOT flipped for these kinds
    u = db.get(User, user.id)
    assert u.gmail_disconnected is False


def test_drain_auth_revoked_flips_gmail_disconnected(
    db: Session, user: User, company: Company, to_contact: Contact
) -> None:
    item = _make_item(db, user=user, company=company, to_contact=to_contact)
    failure = gmail_send.SendResult(
        ok=False,
        failure_kind="gmail_auth_revoked",
        gmail_error_code="invalid_grant",
        error_message="bad refresh token",
    )

    with _stub_send(failure):
        summary = send_worker.drain_due_items(db)

    assert summary.failed == 1
    db.expire_all()
    u = db.get(User, user.id)
    assert u.gmail_disconnected is True

    item = db.get(TodayBatchItem, item.id)
    assert item.status == "failed"


def test_drain_truncates_long_error_message(
    db: Session, user: User, company: Company, to_contact: Contact
) -> None:
    _make_item(db, user=user, company=company, to_contact=to_contact)
    huge = "z" * 5000
    failure = gmail_send.SendResult(
        ok=False, failure_kind="unknown", gmail_error_code=None, error_message=huge
    )
    with _stub_send(failure):
        send_worker.drain_due_items(db)

    row = db.scalar(select(EmailFailure))
    assert row is not None
    assert len(row.error_message) == 2000


# ─────────────────────────── skip paths ───────────────────────────


def test_drain_skips_suspended_user(
    db: Session, user: User, company: Company, to_contact: Contact
) -> None:
    user.is_suspended = True
    db.commit()
    item = _make_item(db, user=user, company=company, to_contact=to_contact)
    with _stub_send(gmail_send.SendResult(ok=True, gmail_message_id="m")):
        summary = send_worker.drain_due_items(db)

    assert summary.skipped == 1
    assert summary.sent == 0
    item = db.get(TodayBatchItem, item.id)
    assert item.status == "skipped"
    assert item.skip_reason == "user_suspended"
    # no failure row
    assert db.scalar(select(EmailFailure)) is None


def test_drain_skips_disconnected_user(
    db: Session, user: User, company: Company, to_contact: Contact
) -> None:
    user.gmail_disconnected = True
    db.commit()
    item = _make_item(db, user=user, company=company, to_contact=to_contact)
    with _stub_send(gmail_send.SendResult(ok=True, gmail_message_id="m")):
        summary = send_worker.drain_due_items(db)
    assert summary.skipped == 1
    item = db.get(TodayBatchItem, item.id)
    assert item.status == "skipped"
    assert item.skip_reason == "gmail_disconnected"


def test_drain_skips_when_contact_has_no_email(
    db: Session, user: User, company: Company, to_contact: Contact
) -> None:
    to_contact.email = None
    db.commit()
    item = _make_item(db, user=user, company=company, to_contact=to_contact)
    with _stub_send(gmail_send.SendResult(ok=True, gmail_message_id="m")):
        summary = send_worker.drain_due_items(db)
    assert summary.skipped == 1
    item = db.get(TodayBatchItem, item.id)
    assert item.status == "skipped"
    assert item.skip_reason == "contact_missing_email"


# ─────────────────────────── filtering ───────────────────────────


def test_drain_ignores_future_send_time(
    db: Session, user: User, company: Company, to_contact: Contact
) -> None:
    """send_time in the future → not picked up."""
    item = _make_item(
        db, user=user, company=company, to_contact=to_contact,
        send_time_offset_seconds=3600,
    )
    with _stub_send(gmail_send.SendResult(ok=True, gmail_message_id="m")):
        summary = send_worker.drain_due_items(db)
    assert summary.attempted == 0
    assert db.get(TodayBatchItem, item.id).status == "ready"


def test_drain_ignores_already_sent_item(
    db: Session, user: User, company: Company, to_contact: Contact
) -> None:
    item = _make_item(db, user=user, company=company, to_contact=to_contact, status="sent")
    with _stub_send(gmail_send.SendResult(ok=True, gmail_message_id="m")):
        summary = send_worker.drain_due_items(db)
    assert summary.attempted == 0
    # Still 'sent', no second send_queue row, sent_today not bumped.
    db.expire_all()
    assert db.get(TodayBatchItem, item.id).status == "sent"
    assert (db.get(User, user.id).sent_today or 0) == 0


# ─────────────────────────── idempotency ───────────────────────────


def test_drain_twice_does_not_double_send(
    db: Session, user: User, company: Company, to_contact: Contact
) -> None:
    _make_item(db, user=user, company=company, to_contact=to_contact)
    success = gmail_send.SendResult(ok=True, gmail_message_id="m1")
    with _stub_send(success):
        first = send_worker.drain_due_items(db)
    # Second pass: item is already 'sent', so no work.
    with _stub_send(success):
        second = send_worker.drain_due_items(db)

    assert first.sent == 1
    assert second.attempted == 0
    assert second.sent == 0

    rows = db.scalars(select(SendQueue)).all()
    assert len(list(rows)) == 1
    assert (db.get(User, user.id).sent_today or 0) == 1


# ─────────────────────────── missing-creds path ───────────────────────────


def test_drain_records_failure_when_no_refresh_token(
    db: Session, user: User, company: Company, to_contact: Contact
) -> None:
    # User has tokens encrypted with the real Fernet key, but for this test
    # we force get_user_credentials to raise — the worker should treat it as
    # an auth-revoked failure.
    from app.services.google_oauth import OAuthError

    item = _make_item(db, user=user, company=company, to_contact=to_contact)
    with patch.object(
        send_worker, "get_user_credentials", side_effect=OAuthError("missing_refresh_token")
    ):
        summary = send_worker.drain_due_items(db)

    assert summary.failed == 1
    assert summary.failures_by_kind == {"gmail_auth_revoked": 1}
    db.expire_all()
    item = db.get(TodayBatchItem, item.id)
    assert item.status == "failed"
    u = db.get(User, user.id)
    assert u.gmail_disconnected is True


# ─────────────────────────── lock side effect ───────────────────────────


def test_successful_send_extends_existing_global_lock(
    db: Session, user: User, company: Company, to_contact: Contact
) -> None:
    locks_repo.upsert_global_lock(db, "acme.com", locked_by_user_id=user.id)
    db.commit()
    original = db.get(GlobalContactLock, "acme.com").locked_until

    _make_item(db, user=user, company=company, to_contact=to_contact)
    success = gmail_send.SendResult(ok=True, gmail_message_id="m")
    with _stub_send(success):
        send_worker.drain_due_items(db)

    db.expire_all()
    updated = db.get(GlobalContactLock, "acme.com").locked_until
    assert updated >= original


# ─────────────────────────── daily-cap enforcement ───────────────────────────


def test_drain_skips_when_user_already_at_cap(
    db: Session, user: User, company: Company, to_contact: Contact
) -> None:
    """A user already at their cap must not get another send. The item stays
    'ready' (not terminal 'skipped') so it sends after the next daily reset.
    daily_limit set explicitly so the test is deterministic regardless of the
    tier-default."""
    user.daily_limit = 5
    user.sent_today = 5  # at cap
    db.commit()
    item = _make_item(db, user=user, company=company, to_contact=to_contact)

    success = gmail_send.SendResult(ok=True, gmail_message_id="m")
    with _stub_send(success):
        summary = send_worker.drain_due_items(db)

    assert summary.attempted == 1
    assert summary.sent == 0
    assert summary.skipped == 1

    db.expire_all()
    item = db.get(TodayBatchItem, item.id)
    assert item.status == "ready"  # NOT marked skipped/sent — retries after reset
    u = db.get(User, user.id)
    assert u.sent_today == 5  # unchanged


def test_drain_enforces_cap_mid_run_across_items(
    db: Session, user: User, company: Company, to_contact: Contact
) -> None:
    """With a cap of 2 and 3 ready items for the same user, exactly 2 send and
    the 3rd is held (cap reached as sent_today climbs within the run)."""
    user.daily_limit = 2  # override → cap=2
    user.sent_today = 0
    db.commit()

    # Three distinct companies/contacts so each item is its own send.
    items = []
    for i in range(3):
        co = Company(domain=f"c{i}.com", name=f"C{i}", source="seed")
        db.add(co)
        db.flush()
        ct = Contact(company_id=co.id, email=f"x@c{i}.com", name=f"X{i}")
        db.add(ct)
        db.flush()
        items.append(_make_item(db, user=user, company=co, to_contact=ct))

    success = gmail_send.SendResult(ok=True, gmail_message_id="m")
    with _stub_send(success):
        summary = send_worker.drain_due_items(db)

    assert summary.sent == 2
    assert summary.skipped == 1

    db.expire_all()
    u = db.get(User, user.id)
    assert u.sent_today == 2
    statuses = sorted(db.get(TodayBatchItem, it.id).status for it in items)
    assert statuses == ["ready", "sent", "sent"]  # one held at 'ready'
