"""Tests for the B5.6 reply ingestor — orchestration only.

We mock out the Gmail History API at the `gmail_reply_fetcher.fetch_new_replies`
boundary so these tests stay focused on:

  - Matching replies to send_queue rows by gmail_thread_id + user_id.
  - Routing to the correct lock-writing path (per-user 30d vs. platform
    permanent) via the existing `locks_svc.record_reply_from_company`.
  - Status flips on `send_queue` (→ REPLIED) and `today_batch_items` (→ replied).
  - Skip semantics for suspended / gmail_disconnected users.
  - Idempotency (re-running with the same reply doesn't double-write).
  - History cursor advancement (`user.gmail_history_id`).
  - Auth-revoked from the fetcher flips `user.gmail_disconnected`.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Company,
    Contact,
    PlatformCompanyLock,
    SendQueue,
    TodayBatchItem,
    User,
    UserCompanyLock,
)
from app.services import gmail_reply_fetcher
from app.services import reply_ingestor as ri
from tests.conftest import _make_user

# ─────────────────────────── helpers ───────────────────────────


def _seed_company_contact(
    db: Session, *, domain: str = "acme.com", name: str = "Acme Inc"
) -> tuple[Company, Contact]:
    company = Company(domain=domain, name=name, source="test")
    db.add(company)
    db.flush()
    contact = Contact(
        company_id=company.id,
        name="John Doe",
        email=f"john@{domain}",
        role="Engineer",
    )
    db.add(contact)
    db.commit()
    return company, contact


def _seed_send_queue_row(
    db: Session,
    *,
    user: User,
    company: Company,
    contact: Contact,
    thread_id: str,
    today_batch_item: TodayBatchItem | None = None,
    sent_offset_minutes: int = -10,
) -> SendQueue:
    sq = SendQueue(
        user_id=user.id,
        contact_id=contact.id,
        to_contact_id=contact.id,
        cc_contact_ids="[]",
        company_domain=company.domain,
        subject="Quick intro",
        body_text="Hi John, ...",
        kind="INITIAL",
        scheduled_for=datetime.now(UTC),
        status="SENT",
        sent_at=datetime.now(UTC) + timedelta(minutes=sent_offset_minutes),
        gmail_message_id=f"msg-out-{thread_id}",
        gmail_thread_id=thread_id,
        today_batch_item_id=today_batch_item.id if today_batch_item else None,
    )
    db.add(sq)
    db.commit()
    return sq


def _seed_today_batch_item(
    db: Session, *, user: User, company: Company, contact: Contact
) -> TodayBatchItem:
    item = TodayBatchItem(
        user_id=user.id,
        batch_date=datetime.now(UTC).date(),
        company_id=company.id,
        company_domain=company.domain,
        to_contact_id=contact.id,
        cc_contact_ids="[]",
        subject="Quick intro",
        body="Hi John, ...",
        status="sent",
        send_time=datetime.now(UTC) - timedelta(minutes=15),
    )
    db.add(item)
    db.commit()
    return item


def _reply(
    *,
    thread_id: str,
    body: str = "Sounds good!",
    msg_id: str = "msg-in-1",
    is_bounce: bool = False,
) -> gmail_reply_fetcher.FetchedReply:
    return gmail_reply_fetcher.FetchedReply(
        gmail_message_id=msg_id,
        gmail_thread_id=thread_id,
        from_email="mailer-daemon@googlemail.com" if is_bounce else "john@acme.com",
        from_domain="acme.com",
        subject="Delivery Status Notification (Failure)" if is_bounce else "Re: Quick intro",
        body_text=body,
        internal_date=datetime.now(UTC),
        is_bounce=is_bounce,
    )


# ─────────────────────────── skip semantics ───────────────────────────


def test_suspended_user_skipped(db: Session) -> None:
    user = _make_user(db, email="u@x.com", tier="free")
    user.is_suspended = True
    db.commit()

    summary = ri.ingest_replies_for_user(db, user)
    assert summary.error_kind == "skipped_suspended"
    assert summary.processed == 0


def test_disconnected_user_skipped(db: Session) -> None:
    user = _make_user(db, email="u@x.com", tier="free")
    user.gmail_disconnected = True
    db.commit()

    summary = ri.ingest_replies_for_user(db, user)
    assert summary.error_kind == "skipped_disconnected"
    assert summary.processed == 0


# ─────────────────────────── regular reply → per-user lock ───────────────────────────


def test_regular_reply_writes_user_company_lock_and_flips_status(db: Session) -> None:
    user = _make_user(db, email="u@x.com", tier="free")
    company, contact = _seed_company_contact(db)
    tbi = _seed_today_batch_item(db, user=user, company=company, contact=contact)
    sq = _seed_send_queue_row(
        db, user=user, company=company, contact=contact, thread_id="thr-1", today_batch_item=tbi
    )

    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher, "fetch_new_replies",
        return_value=([_reply(thread_id="thr-1", body="Thanks, will check it out.")], 12345),
    ):
        summary = ri.ingest_replies_for_user(db, user)

    assert summary.replies_matched == 1
    assert summary.user_reply_locks_written == 1
    assert summary.explicit_stops == 0

    db.refresh(sq)
    db.refresh(tbi)
    db.refresh(user)
    assert sq.status == "REPLIED"
    assert sq.reply_is_explicit_stop is False
    assert sq.replied_at is not None
    assert tbi.status == "replied"
    assert user.gmail_history_id == 12345

    # Per-user lock written, platform-permanent NOT written.
    lock = db.scalar(select(UserCompanyLock).where(UserCompanyLock.user_id == user.id))
    assert lock is not None
    assert lock.is_permanent is False
    perm = db.scalar(select(PlatformCompanyLock).where(PlatformCompanyLock.company_domain == "acme.com"))
    assert perm is None


# ─────────────────────────── explicit stop → platform-wide lock ───────────────────────────


def test_explicit_stop_writes_platform_permanent_lock(db: Session) -> None:
    user = _make_user(db, email="u@x.com", tier="free")
    company, contact = _seed_company_contact(db)
    _seed_send_queue_row(
        db, user=user, company=company, contact=contact, thread_id="thr-stop"
    )

    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher, "fetch_new_replies",
        return_value=([_reply(thread_id="thr-stop", body="Please unsubscribe me.")], 999),
    ):
        summary = ri.ingest_replies_for_user(db, user)

    assert summary.explicit_stops == 1
    assert summary.user_reply_locks_written == 0

    perm = db.scalar(select(PlatformCompanyLock).where(PlatformCompanyLock.company_domain == "acme.com"))
    assert perm is not None


# ─────────────────────────── unmatched threads ignored ───────────────────────────


def test_unmatched_thread_id_is_ignored(db: Session) -> None:
    user = _make_user(db, email="u@x.com", tier="free")
    _seed_company_contact(db)  # No send_queue row at all.

    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher, "fetch_new_replies",
        return_value=([_reply(thread_id="ghost-thread")], 1),
    ):
        summary = ri.ingest_replies_for_user(db, user)

    assert summary.processed == 1
    assert summary.replies_matched == 0
    assert summary.explicit_stops == 0


# ─────────────────────────── idempotency ───────────────────────────


def test_rerun_with_same_reply_does_not_double_write(db: Session) -> None:
    user = _make_user(db, email="u@x.com", tier="free")
    company, contact = _seed_company_contact(db)
    sq = _seed_send_queue_row(db, user=user, company=company, contact=contact, thread_id="thr-idem")

    reply = _reply(thread_id="thr-idem", body="Thanks!", msg_id="msg-in-7")

    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher, "fetch_new_replies", return_value=([reply], 100),
    ):
        first = ri.ingest_replies_for_user(db, user)
    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher, "fetch_new_replies", return_value=([reply], 100),
    ):
        second = ri.ingest_replies_for_user(db, user)

    assert first.user_reply_locks_written == 1
    assert second.user_reply_locks_written == 0  # already REPLIED → guard kicks in
    db.refresh(sq)
    # Lock was extended (rolling) on first pass; second pass guard short-circuits.
    locks = list(db.scalars(select(UserCompanyLock).where(UserCompanyLock.user_id == user.id)).all())
    assert len(locks) == 1


# ─────────────────────────── history cursor advancement ───────────────────────────


def test_history_cursor_advances_even_on_zero_replies(db: Session) -> None:
    """Bootstrap case: fetcher returns ([], latest_history_id) so next run only
    processes truly new messages."""
    user = _make_user(db, email="u@x.com", tier="free")

    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher, "fetch_new_replies", return_value=([], 42),
    ):
        summary = ri.ingest_replies_for_user(db, user)

    db.refresh(user)
    assert user.gmail_history_id == 42
    assert summary.processed == 0


def test_history_cursor_does_not_regress(db: Session) -> None:
    user = _make_user(db, email="u@x.com", tier="free")
    user.gmail_history_id = 999
    db.commit()

    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher, "fetch_new_replies", return_value=([], 100),
    ):
        ri.ingest_replies_for_user(db, user)

    db.refresh(user)
    assert user.gmail_history_id == 999  # not lowered


# ─────────────────────────── fetcher errors ───────────────────────────


def test_auth_revoked_flips_gmail_disconnected(db: Session) -> None:
    user = _make_user(db, email="u@x.com", tier="free")

    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher, "fetch_new_replies",
        side_effect=gmail_reply_fetcher.FetchError("gmail_auth_revoked", "401"),
    ):
        summary = ri.ingest_replies_for_user(db, user)

    db.refresh(user)
    assert user.gmail_disconnected is True
    assert summary.error_kind == "gmail_auth_revoked"


@pytest.mark.parametrize("kind", ["transient", "quota_exceeded", "unknown"])
def test_transient_errors_do_not_disconnect_user(db: Session, kind: str) -> None:
    user = _make_user(db, email="u@x.com", tier="free")

    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher, "fetch_new_replies",
        side_effect=gmail_reply_fetcher.FetchError(kind, ""),
    ):
        summary = ri.ingest_replies_for_user(db, user)

    db.refresh(user)
    assert user.gmail_disconnected is False
    assert summary.error_kind == kind


# ─────────────────────────── platform-permanent already exists ───────────────────────────


def test_platform_permanent_lock_is_upserted_not_duplicated(db: Session) -> None:
    user = _make_user(db, email="u@x.com", tier="free")
    company, contact = _seed_company_contact(db)
    _seed_send_queue_row(db, user=user, company=company, contact=contact, thread_id="thr-perm")

    # Two stop-language replies in one ingest run.
    replies = [
        _reply(thread_id="thr-perm", body="Stop emailing me.", msg_id="m1"),
        _reply(thread_id="thr-perm", body="Stop emailing me.", msg_id="m2"),
    ]
    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher, "fetch_new_replies", return_value=(replies, 1),
    ):
        ri.ingest_replies_for_user(db, user)

    perms = list(
        db.scalars(select(PlatformCompanyLock).where(PlatformCompanyLock.company_domain == "acme.com")).all()
    )
    assert len(perms) == 1


# ─────────────────────────── bounce handling ───────────────────────────


def test_bounce_marks_contact_invalid_not_reply_lock(db: Session) -> None:
    """A bounce on the send thread must invalidate the contact, NOT write a
    reply lock."""
    user = _make_user(db, email="u@x.com", tier="free")
    company, contact = _seed_company_contact(db)
    sq = _seed_send_queue_row(db, user=user, company=company, contact=contact, thread_id="thr-b")

    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher, "fetch_new_replies",
        return_value=([_reply(thread_id="thr-b", is_bounce=True)], 5),
    ):
        summary = ri.ingest_replies_for_user(db, user)

    assert summary.bounces == 1
    assert summary.replies_matched == 0
    assert summary.user_reply_locks_written == 0

    db.refresh(contact)
    db.refresh(sq)
    assert contact.is_invalid is True
    assert sq.status == "BOUNCED"

    # No per-user reply lock written.
    from app.models import UserCompanyLock as _UCL

    assert db.scalar(select(_UCL).where(_UCL.user_id == user.id)) is None


def test_bounce_on_scraped_contact_still_invalidates(db: Session) -> None:
    """Scraped contact (carries scraped_pattern) bounces → invalidated + logged
    as a scraper-retry signal. (Alternate-pattern generation ships with the
    scraper; here we just confirm it's invalidated, not reply-locked.)"""
    user = _make_user(db, email="u@x.com", tier="free")
    company, contact = _seed_company_contact(db)
    contact.scraped_pattern = "firstname.lastname"
    db.commit()
    _seed_send_queue_row(db, user=user, company=company, contact=contact, thread_id="thr-sb")

    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher, "fetch_new_replies",
        return_value=([_reply(thread_id="thr-sb", is_bounce=True)], 9),
    ):
        summary = ri.ingest_replies_for_user(db, user)

    assert summary.bounces == 1
    db.refresh(contact)
    # Scraped contact with patterns still ahead → ADVANCED (not invalidated)
    # so the scraper can retry the next email guess.
    assert contact.is_invalid is False


def test_scraped_bounce_advances_to_next_pattern(db: Session) -> None:
    """A scraped contact whose guess bounces gets its email advanced to the
    next pattern in EMAIL_PATTERN_ORDER and stays in rotation (NOT invalidated).
    Decoupled from the specific order so a reshuffle doesn't break the test."""
    from app.services.email_patterns import EMAIL_PATTERN_ORDER

    user = _make_user(db, email="u@x.com", tier="free")
    company = Company(domain="acme.com", name="Acme", source="scrape")
    db.add(company)
    db.flush()
    # Start at the FIRST pattern so there's always a 'next' to advance to.
    first_pattern = EMAIL_PATTERN_ORDER[0]
    contact = Contact(
        company_id=company.id,
        name="Akanksha Puri",
        email="placeholder@acme.com",
        scraped_pattern=first_pattern,
    )
    db.add(contact)
    db.commit()
    _seed_send_queue_row(db, user=user, company=company, contact=contact, thread_id="thr-sp")

    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher, "fetch_new_replies",
        return_value=([_reply(thread_id="thr-sp", is_bounce=True)], 3),
    ):
        summary = ri.ingest_replies_for_user(db, user)

    assert summary.bounces == 1
    db.refresh(contact)
    # Advanced to SOMETHING that comes later in the order, stayed valid.
    assert contact.scraped_pattern != first_pattern
    assert contact.scraped_pattern in EMAIL_PATTERN_ORDER
    assert contact.is_invalid is False
    assert contact.email != "placeholder@acme.com"


def test_scraped_bounce_self_collision_is_not_treated_as_collision(
    db: Session,
) -> None:
    """Regression: when the next pattern's address happens to equal the
    contact's CURRENT email (e.g. test seeded with email/pattern that don't
    match, or two patterns yield the same local-part for a given name), the
    collision lookup used to match the contact against itself and wrongly bail
    to 'patterns exhausted' — invalidating with one guess still left."""
    user = _make_user(db, email="u@x.com", tier="free")
    company = Company(domain="acme.com", name="Acme", source="scrape")
    db.add(company)
    db.flush()
    # Hand-pick the collision: current email = 'john@acme.com', recorded as
    # scraped from 'firstname.lastname'. With the pre-reshuffle order the next
    # pattern is 'firstname' which builds 'john@acme.com' — same as current.
    # Pre-fix, that self-match invalidated the contact; post-fix it should
    # advance cleanly (the self-row isn't a real collision).
    contact = Contact(
        company_id=company.id,
        name="John Doe",
        email="john@acme.com",
        scraped_pattern="firstname.lastname",
    )
    db.add(contact)
    db.commit()
    _seed_send_queue_row(
        db, user=user, company=company, contact=contact, thread_id="thr-self"
    )

    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher,
        "fetch_new_replies",
        return_value=([_reply(thread_id="thr-self", is_bounce=True)], 9),
    ):
        summary = ri.ingest_replies_for_user(db, user)

    assert summary.bounces == 1
    db.refresh(contact)
    # MUST stay valid — there are still patterns ahead of 'firstname.lastname'.
    assert contact.is_invalid is False
    assert contact.invalid_reason is None
    # The scraped_pattern must have ADVANCED past 'firstname.lastname'.
    assert contact.scraped_pattern != "firstname.lastname"


def test_scraped_bounce_does_collide_with_a_different_contact(
    db: Session,
) -> None:
    """The collision check still fires against a DIFFERENT contact already
    holding the next-pattern address — we don't want to clobber a real row."""
    user = _make_user(db, email="u@x.com", tier="free")
    company = Company(domain="acme.com", name="Acme", source="scrape")
    db.add(company)
    db.flush()
    # Pre-seed a different contact at the address the next pattern would yield.
    Contact_other = Contact(
        company_id=company.id, name="Squatter", email="john@acme.com"
    )
    db.add(Contact_other)
    contact = Contact(
        company_id=company.id,
        name="John Doe",
        email="john.doe@acme.com",
        scraped_pattern="firstname.lastname",
    )
    db.add(contact)
    db.commit()
    _seed_send_queue_row(
        db, user=user, company=company, contact=contact, thread_id="thr-collide"
    )

    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher,
        "fetch_new_replies",
        return_value=([_reply(thread_id="thr-collide", is_bounce=True)], 9),
    ):
        ri.ingest_replies_for_user(db, user)

    db.refresh(contact)
    # The next pattern (firstname) → john@acme.com collides with Squatter, so
    # _handle_bounce must NOT advance to it. v0 behavior is to give up
    # (single-shot advancement); future work could walk the order. The point
    # of THIS test is just that we never clobber the other contact.
    other = db.get(Contact, Contact_other.id)
    assert other is not None
    assert other.name == "Squatter"
    assert other.email == "john@acme.com"


def test_scraped_bounce_invalidates_when_patterns_exhausted(db: Session) -> None:
    user = _make_user(db, email="u@x.com", tier="free")
    company = Company(domain="acme.com", name="Acme", source="scrape")
    db.add(company)
    db.flush()
    contact = Contact(
        company_id=company.id,
        name="Akanksha Puri",
        email="puri@acme.com",
        scraped_pattern="lastname",  # the last pattern in the order
    )
    db.add(contact)
    db.commit()
    _seed_send_queue_row(db, user=user, company=company, contact=contact, thread_id="thr-ex")

    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher, "fetch_new_replies",
        return_value=([_reply(thread_id="thr-ex", is_bounce=True)], 4),
    ):
        ri.ingest_replies_for_user(db, user)

    db.refresh(contact)
    assert contact.is_invalid is True
    assert contact.invalid_reason == "bounce_patterns_exhausted"


def test_csv_bounce_sets_invalid_reason_bounce(db: Session) -> None:
    """A non-scraped (CSV) contact bounce → invalid + reason 'bounce' for the
    admin review list. No pattern retry."""
    user = _make_user(db, email="u@x.com", tier="free")
    company, contact = _seed_company_contact(db)  # no scraped_pattern
    _seed_send_queue_row(db, user=user, company=company, contact=contact, thread_id="thr-csv")

    with patch.object(ri, "get_user_credentials", return_value=object()), patch.object(
        gmail_reply_fetcher, "fetch_new_replies",
        return_value=([_reply(thread_id="thr-csv", is_bounce=True)], 6),
    ):
        ri.ingest_replies_for_user(db, user)

    db.refresh(contact)
    assert contact.is_invalid is True
    assert contact.invalid_reason == "bounce"
    assert contact.scraped_pattern is None
