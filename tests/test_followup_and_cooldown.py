"""Tests for the 0018 features: 30-day per-user contact cooldown + the
follow-up planner / threaded Gmail send / reply-cancels-followups path.

Mocked at the gmail_send seam (same pattern as test_send_worker.py) so we
don't depend on real Google credentials.
"""
from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.time import ensure_utc, utcnow
from app.models import (
    Company,
    Contact,
    SendQueue,
    TodayBatchItem,
    User,
    UserContactCooldown,
)
from app.repositories import user_contact_cooldown as cooldown_repo
from app.services import (
    followup_planner,
    gmail_reply_fetcher,
    gmail_send,
    reply_ingestor,
    send_worker,
)
from app.services.today_picker import (
    ContactCandidate,
    pick_companies_for_user,
)
from tests.conftest import _make_user

# ─────────────────────────── fixtures ───────────────────────────


@pytest.fixture
def user(db: Session) -> User:
    u = _make_user(db, email="cool@example.com", google_sub="g-cool", tier="free")
    u.google_refresh_token = "encrypted-refresh"
    u.google_access_token = "encrypted-access"
    u.full_name = "Cool Person"
    db.commit()
    return u


@pytest.fixture
def company(db: Session) -> Company:
    c = Company(domain="acme.com", name="Acme Inc", source="seed")
    db.add(c)
    db.commit()
    return c


@pytest.fixture
def contact(db: Session, company: Company) -> Contact:
    c = Contact(company_id=company.id, email="ann@acme.com", name="Ann")
    db.add(c)
    db.commit()
    return c


# ─────────────────────────── 30-day cooldown ───────────────────────────


def test_cooldown_repo_upsert_writes_one_row_per_contact(
    db: Session, user: User, contact: Contact
) -> None:
    now = utcnow()
    count = cooldown_repo.upsert_after_send(
        db,
        user_id=user.id,
        contact_ids=[contact.id, contact.id],  # duplicate squashed
        now=now,
        cooldown_days=30,
    )
    db.commit()

    assert count == 1
    rows = db.scalars(select(UserContactCooldown)).all()
    assert len(rows) == 1
    assert rows[0].user_id == user.id
    assert rows[0].contact_id == contact.id
    # 30 days from now (within a second). libsql strips tzinfo on round-trip,
    # so re-attach UTC at the boundary.
    delta = (ensure_utc(rows[0].cooldown_until) - now) - timedelta(days=30)
    assert abs(delta.total_seconds()) < 1


def test_cooldown_repo_lists_only_active_blocks(
    db: Session, user: User, contact: Contact
) -> None:
    now = utcnow()
    expired = UserContactCooldown(
        user_id=user.id,
        contact_id=contact.id,
        last_sent_at=now - timedelta(days=40),
        cooldown_until=now - timedelta(days=10),  # expired
    )
    db.add(expired)
    db.commit()

    blocked = cooldown_repo.list_blocked_contact_ids(db, user.id, now=now)
    assert blocked == set()  # expired → not blocked


def test_picker_excludes_contacts_in_cooldown(
    db: Session, user: User, company: Company
) -> None:
    """The picker drops blocked contacts BEFORE grouping by company — a company
    keeps eligibility iff at least one un-cooled contact survives."""
    other = Contact(company_id=company.id, email="ben@acme.com", name="Ben")
    db.add(other)
    db.commit()

    candidates = [
        ContactCandidate(
            contact_id=1, company_id=company.id, company_domain=company.domain,
            email="a@acme.com", is_invalid=False,
        ),
        ContactCandidate(
            contact_id=2, company_id=company.id, company_domain=company.domain,
            email="b@acme.com", is_invalid=False,
        ),
    ]
    from random import Random

    from app.core.time import utcnow as _now

    picks = pick_companies_for_user(
        user_id=user.id,
        candidates=candidates,
        cap=5,
        excluded_domains=set(),
        blocked_user_lock_domains=set(),
        blocked_platform_permanent_domains=set(),
        cooldown_domains=set(),
        blocked_contact_ids={1},  # contact 1 in cooldown — contact 2 still eligible
        batch_date=_now().date(),
        tier="free",
        rng=Random(7),
    )
    assert len(picks) == 1
    assert picks[0].to_contact_id == 2


def test_picker_skips_company_entirely_when_all_contacts_blocked(
    db: Session, user: User, company: Company
) -> None:
    candidates = [
        ContactCandidate(
            contact_id=1, company_id=company.id, company_domain=company.domain,
            email="a@acme.com", is_invalid=False,
        ),
    ]
    from random import Random

    from app.core.time import utcnow as _now

    picks = pick_companies_for_user(
        user_id=user.id,
        candidates=candidates,
        cap=5,
        excluded_domains=set(),
        blocked_user_lock_domains=set(),
        blocked_platform_permanent_domains=set(),
        cooldown_domains=set(),
        blocked_contact_ids={1},  # the ONLY contact at this domain
        batch_date=_now().date(),
        tier="free",
        rng=Random(7),
    )
    assert picks == []


def _stub_send_ok():
    """Patch gmail_send + creds for send_worker tests."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch.object(
            gmail_send,
            "send_email",
            return_value=gmail_send.SendResult(
                ok=True,
                gmail_message_id="m1",
                gmail_thread_id="t1",
                rfc822_message_id="<auto@knock.app>",
            ),
        )
    )
    stack.enter_context(
        patch.object(send_worker, "get_user_credentials", return_value=object())
    )
    return stack


def test_send_worker_writes_cooldown_row_for_to_and_cc(
    db: Session, user: User, company: Company, contact: Contact
) -> None:
    cc_contact = Contact(company_id=company.id, email="cc@acme.com", name="CC")
    db.add(cc_contact)
    db.commit()

    item = TodayBatchItem(
        user_id=user.id,
        batch_date=utcnow().date(),
        company_id=company.id,
        company_domain=company.domain,
        to_contact_id=contact.id,
        cc_contact_ids=json.dumps([cc_contact.id]),
        subject="Hello",
        body="Body",
        status="ready",
        send_time=utcnow() - timedelta(seconds=10),
    )
    db.add(item)
    db.commit()

    with _stub_send_ok():
        summary = send_worker.drain_due_items(db)
    assert summary.sent == 1

    # Both the TO and the CC should have a cooldown row.
    rows = db.scalars(
        select(UserContactCooldown).where(UserContactCooldown.user_id == user.id)
    ).all()
    contact_ids_in_cooldown = {r.contact_id for r in rows}
    assert contact_ids_in_cooldown == {contact.id, cc_contact.id}
    # The cooldown is ~30 days out. libsql strips tzinfo on storage so both
    # come back naive — direct subtraction works.
    for r in rows:
        delta = r.cooldown_until - r.last_sent_at
        assert abs((delta - timedelta(days=settings.USER_CONTACT_COOLDOWN_DAYS)).total_seconds()) < 5


def test_send_worker_persists_rfc822_message_id(
    db: Session, user: User, company: Company, contact: Contact
) -> None:
    item = TodayBatchItem(
        user_id=user.id,
        batch_date=utcnow().date(),
        company_id=company.id,
        company_domain=company.domain,
        to_contact_id=contact.id,
        cc_contact_ids="[]",
        subject="Hello",
        body="Body",
        status="ready",
        send_time=utcnow() - timedelta(seconds=10),
    )
    db.add(item)
    db.commit()

    with _stub_send_ok():
        send_worker.drain_due_items(db)

    sq = db.scalar(select(SendQueue).where(SendQueue.user_id == user.id))
    assert sq is not None
    assert sq.rfc822_message_id == "<auto@knock.app>"
    assert sq.kind == "INITIAL"


# ─────────────────────────── gmail_send threading ───────────────────────────


def test_build_mime_self_generates_message_id() -> None:
    msg, rfc822_id = gmail_send.build_mime(
        sender_email="me@knock.app",
        sender_name=None,
        to_email="them@acme.com",
        cc_emails=[],
        subject="hi",
        body_text="hello",
    )
    # We control the Message-ID; it must be set on the outgoing MIME and
    # returned to the caller (so the worker can persist it).
    assert msg["Message-ID"] == rfc822_id
    assert rfc822_id.startswith("<") and rfc822_id.endswith(">")
    # Domain part of the generated id matches the sender domain.
    assert "knock.app" in rfc822_id


def test_build_mime_followup_sets_in_reply_to_and_references() -> None:
    msg, _ = gmail_send.build_mime(
        sender_email="me@knock.app",
        sender_name=None,
        to_email="them@acme.com",
        cc_emails=[],
        subject="Re: hi",
        body_text="bump",
        in_reply_to_rfc822_id="<orig@knock.app>",
    )
    assert msg["In-Reply-To"] == "<orig@knock.app>"
    # References defaults to the immediate parent when no chain passed.
    assert msg["References"] == "<orig@knock.app>"


def test_send_followup_passes_threadId_to_gmail_api() -> None:
    """The Gmail API body must include 'threadId' for thread continuity."""
    sent_bodies: list[dict] = []

    class FakeMessages:
        def send(self, *, userId, body):
            sent_bodies.append(body)

            class FakeReq:
                def execute(self_inner):
                    return {"id": "m_new", "threadId": "t_existing"}

            return FakeReq()

    class FakeUsers:
        def messages(self_inner):
            return FakeMessages()

    class FakeService:
        def users(self_inner):
            return FakeUsers()

    with patch.object(gmail_send, "_build_service", return_value=FakeService()):
        result = gmail_send.send_followup(
            object(),  # creds (unused — service is patched)
            sender_email="me@knock.app",
            sender_name=None,
            to_email="them@acme.com",
            cc_emails=[],
            subject="Re: hi",
            body_text="bump",
            gmail_thread_id="t_existing",
            in_reply_to_rfc822_id="<orig@knock.app>",
        )

    assert result.ok
    assert result.gmail_thread_id == "t_existing"
    assert len(sent_bodies) == 1
    assert sent_bodies[0]["threadId"] == "t_existing"


# ─────────────────────────── follow-up planner ───────────────────────────


def _seed_initial_sent_send_queue(
    db: Session,
    *,
    user: User,
    company: Company,
    contact: Contact,
    sent_days_ago: int,
    replied: bool = False,
) -> tuple[SendQueue, TodayBatchItem]:
    """Create a SENT initial send + its source today_batch_item."""
    sent_at = utcnow() - timedelta(days=sent_days_ago)
    tbi = TodayBatchItem(
        user_id=user.id,
        batch_date=sent_at.date(),
        company_id=company.id,
        company_domain=company.domain,
        to_contact_id=contact.id,
        cc_contact_ids="[]",
        subject="Original",
        body="Original body",
        status="sent",
        send_time=sent_at,
        kind="initial",
    )
    db.add(tbi)
    db.flush()
    sq = SendQueue(
        user_id=user.id,
        contact_id=contact.id,
        today_batch_item_id=tbi.id,
        to_contact_id=contact.id,
        cc_contact_ids="[]",
        company_domain=company.domain,
        subject="Original",
        body_text="Original body",
        gmail_message_id="m_orig",
        gmail_thread_id="thr_orig",
        rfc822_message_id="<orig@knock.app>",
        kind="INITIAL",
        scheduled_for=sent_at,
        status="SENT",
        sent_at=sent_at,
        replied_at=sent_at if replied else None,
    )
    if replied:
        sq.status = "REPLIED"
    db.add(sq)
    db.commit()
    return sq, tbi


def test_planner_schedules_followup_for_day_4_no_reply(
    db: Session, user: User, company: Company, contact: Contact
) -> None:
    _seed_initial_sent_send_queue(
        db, user=user, company=company, contact=contact, sent_days_ago=5
    )
    today = utcnow().date()

    summary = followup_planner.plan_due_followups(db, today=today)
    assert summary.planned == 1

    tbi = db.scalar(
        select(TodayBatchItem)
        .where(TodayBatchItem.user_id == user.id)
        .where(TodayBatchItem.kind == "followup")
    )
    assert tbi is not None
    assert tbi.subject.startswith("Re: ")
    assert tbi.parent_send_queue_id is not None
    assert tbi.followup_index == 1


def test_planner_skips_threads_with_a_reply(
    db: Session, user: User, company: Company, contact: Contact
) -> None:
    _seed_initial_sent_send_queue(
        db, user=user, company=company, contact=contact, sent_days_ago=5, replied=True
    )
    summary = followup_planner.plan_due_followups(db, today=utcnow().date())
    assert summary.planned == 0


def test_planner_skips_threads_before_delay_window(
    db: Session, user: User, company: Company, contact: Contact
) -> None:
    _seed_initial_sent_send_queue(
        db, user=user, company=company, contact=contact, sent_days_ago=1
    )
    summary = followup_planner.plan_due_followups(db, today=utcnow().date())
    assert summary.planned == 0


def test_planner_idempotent_within_same_day(
    db: Session, user: User, company: Company, contact: Contact
) -> None:
    _seed_initial_sent_send_queue(
        db, user=user, company=company, contact=contact, sent_days_ago=5
    )
    today = utcnow().date()
    first = followup_planner.plan_due_followups(db, today=today)
    second = followup_planner.plan_due_followups(db, today=today)
    assert first.planned == 1
    assert second.planned == 0


# ─────────────────────────── reply cancels follow-ups ───────────────────────────


def test_reply_ingestor_cancels_pending_followups_on_thread(
    db: Session, user: User, company: Company, contact: Contact
) -> None:
    """A reply landing on a thread that has a pending follow-up TBI should mark
    that follow-up as skipped with reason='reply_received'."""
    sq, _orig_tbi = _seed_initial_sent_send_queue(
        db, user=user, company=company, contact=contact, sent_days_ago=5
    )
    # Plan a follow-up — would normally come from the cron.
    followup_planner.plan_due_followups(db, today=utcnow().date())
    pending = db.scalar(
        select(TodayBatchItem)
        .where(TodayBatchItem.user_id == user.id)
        .where(TodayBatchItem.kind == "followup")
    )
    assert pending is not None and pending.status in ("default", "ready")

    # Simulate a reply landing on the thread — using the FetchedReply shape
    # the ingestor expects.
    reply_msg = gmail_reply_fetcher.FetchedReply(
        gmail_message_id="m_reply",
        gmail_thread_id=sq.gmail_thread_id or "",
        from_email="ann@acme.com",
        from_domain="acme.com",
        subject="Re: Original",
        body_text="thanks for reaching out",
        internal_date=utcnow(),
    )
    reply_ingestor._process_reply(
        db,
        user=user,
        reply=reply_msg,
        summary_counters={
            "processed": 0,
            "replies_matched": 0,
            "explicit_stops": 0,
            "user_reply_locks_written": 0,
        },
    )
    db.commit()

    db.expire_all()
    cancelled = db.get(TodayBatchItem, pending.id)
    assert cancelled is not None
    assert cancelled.status == "skipped"
    assert cancelled.skip_reason == "reply_received"
