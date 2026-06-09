"""Follow-up engine — schedules a 2nd / 3rd touch as a TodayBatchItem on the
same Gmail thread as the original send.

Run idempotently (typically daily / every-30min via the scheduler). For each
user, find SendQueue rows where:
  - status='SENT' AND replied_at IS NULL
  - sent_at <= now - FOLLOWUP_DELAY_DAYS
  - this thread (user_id + gmail_thread_id) has fewer than MAX_FOLLOWUPS
    follow-ups already sent
  - and we haven't already planned a follow-up for this parent on today's batch

Writes a TodayBatchItem with kind='followup' + parent_send_queue_id back-link.
The send worker recognises kind='followup' and routes through
`gmail_send.send_followup()` so it lands on the original thread.

The follow-up body comes from `user.followup_template` if set, else a stock
one-line nudge. Brand promise: NEVER AI-generate; either user-authored or a
neutral fixed line.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.config import settings
from app.core.time import utcnow
from app.logging_config import get_logger
from app.models import SendQueue, TodayBatchItem, User
from app.services.today_picker import compute_send_times

log = get_logger("followup_planner")

# Stock fallback when the user hasn't authored their follow-up template yet.
# Kept generic on purpose — the goal is a polite second touch, not a sales
# bump. User can always set their own once we ship the followup-template UI.
_FALLBACK_FOLLOWUP_BODY = (
    "Hi {{first_name}},\n\n"
    "Just wanted to gently bump my note from last week — totally understand if "
    "things are busy. Happy to make this easy: a one-line reply works too.\n\n"
    "Best,\n{{sender_name}}\n"
)


@dataclass
class PlanSummary:
    scanned: int = 0
    planned: int = 0
    skipped_by_reason: dict[str, int] = field(default_factory=dict)


def _add_reply_subject_prefix(subject: str | None) -> str:
    """Prepend 'Re: ' unless the subject already starts with it (case-insensitive)."""
    s = (subject or "").strip()
    return s if s.lower().startswith("re:") else f"Re: {s}"


def _count_followups_on_thread(
    db: OrmSession, *, user_id: int, gmail_thread_id: str
) -> int:
    return int(
        db.scalar(
            select(func.count(SendQueue.id))
            .where(SendQueue.user_id == user_id)
            .where(SendQueue.gmail_thread_id == gmail_thread_id)
            .where(SendQueue.kind == "FOLLOWUP")
            .where(SendQueue.status == "SENT")
        )
        or 0
    )


def _has_followup_already_planned(
    db: OrmSession,
    *,
    user_id: int,
    parent_send_queue_id: int,
    batch_date: date,
) -> bool:
    """Idempotency guard — don't double-schedule a follow-up for the same parent
    in the same daily batch."""
    return (
        db.scalar(
            select(TodayBatchItem.id)
            .where(TodayBatchItem.user_id == user_id)
            .where(TodayBatchItem.parent_send_queue_id == parent_send_queue_id)
            .where(TodayBatchItem.batch_date == batch_date)
        )
        is not None
    )


def _has_initial_for_company_today(
    db: OrmSession, *, user_id: int, company_id: int, batch_date: date
) -> bool:
    """The TBI UC is (user_id, batch_date, company_id) — initial + follow-up to
    the SAME company on the SAME day would collide. Per PM: skip the follow-up
    to today and try again tomorrow — feels less spammy to the recruiter too."""
    return (
        db.scalar(
            select(TodayBatchItem.id)
            .where(TodayBatchItem.user_id == user_id)
            .where(TodayBatchItem.batch_date == batch_date)
            .where(TodayBatchItem.company_id == company_id)
        )
        is not None
    )


def plan_due_followups(
    db: OrmSession, *, today: date, now: datetime | None = None
) -> PlanSummary:
    """Scan for SendQueue rows due for a follow-up; write the corresponding
    TodayBatchItem rows. Caller commits."""
    now = now or utcnow()
    summary = PlanSummary()
    cutoff = now - timedelta(days=settings.FOLLOWUP_DELAY_DAYS)

    # Threads due: most-recent SENT row per (user, thread) older than the cutoff
    # with no reply. We pick the originating row OR the most recent prior
    # follow-up — whichever is the latest activity on the thread — as parent.
    candidates = list(
        db.scalars(
            select(SendQueue)
            .where(SendQueue.status == "SENT")
            .where(SendQueue.replied_at.is_(None))
            .where(SendQueue.sent_at <= cutoff)
            .where(SendQueue.gmail_thread_id.is_not(None))
            .order_by(SendQueue.sent_at.asc())
        ).all()
    )

    # Keep only the most recent sent on each (user, thread) — anything older
    # is superseded.
    latest: dict[tuple[int, str], SendQueue] = {}
    for sq in candidates:
        key = (sq.user_id, sq.gmail_thread_id or "")
        prev = latest.get(key)
        if prev is None or (sq.sent_at or now) > (prev.sent_at or now):
            latest[key] = sq

    for sq in latest.values():
        summary.scanned += 1

        # Already at the max follow-ups for this thread?
        existing_count = _count_followups_on_thread(
            db, user_id=sq.user_id, gmail_thread_id=sq.gmail_thread_id or ""
        )
        if existing_count >= settings.MAX_FOLLOWUPS:
            summary.skipped_by_reason["max_followups_reached"] = (
                summary.skipped_by_reason.get("max_followups_reached", 0) + 1
            )
            continue

        # Already planned this parent in today's batch? (idempotent re-run)
        if _has_followup_already_planned(
            db,
            user_id=sq.user_id,
            parent_send_queue_id=sq.id,
            batch_date=today,
        ):
            summary.skipped_by_reason["already_planned"] = (
                summary.skipped_by_reason.get("already_planned", 0) + 1
            )
            continue

        # Need a TO contact + a company on the parent. Defensive — pre-Phase-5
        # rows may be missing these.
        if (
            sq.to_contact_id is None
            or sq.company_domain is None
            or sq.today_batch_item_id is None
        ):
            summary.skipped_by_reason["parent_missing_fields"] = (
                summary.skipped_by_reason.get("parent_missing_fields", 0) + 1
            )
            continue

        # Same company already has an initial in today's batch — defer.
        parent_tbi = db.get(TodayBatchItem, sq.today_batch_item_id)
        if parent_tbi is None:
            summary.skipped_by_reason["parent_tbi_missing"] = (
                summary.skipped_by_reason.get("parent_tbi_missing", 0) + 1
            )
            continue
        if _has_initial_for_company_today(
            db,
            user_id=sq.user_id,
            company_id=parent_tbi.company_id,
            batch_date=today,
        ):
            summary.skipped_by_reason["company_busy_today"] = (
                summary.skipped_by_reason.get("company_busy_today", 0) + 1
            )
            continue

        user = db.get(User, sq.user_id)
        if user is None or user.is_suspended or user.gmail_disconnected:
            summary.skipped_by_reason["user_unavailable"] = (
                summary.skipped_by_reason.get("user_unavailable", 0) + 1
            )
            continue

        # Build the follow-up TBI.
        tier_for_schedule = user.tier if user.tier in ("free", "paid") else "free"
        send_time = compute_send_times(
            today, 1, tier_for_schedule  # type: ignore[arg-type]
        )[0]
        # Default status: 'ready' if the user has autopilot active and the
        # auto-send-followups preference is on; else 'default' so they review.
        followup_status = (
            "ready"
            if (
                user.autopilot_enabled
                and user.autopilot_paused_at is None
            )
            else "default"
        )

        followup_index = existing_count + 1
        # v1: always use the fallback follow-up body. The user-authored
        # follow-up template lives in a future Settings UI (a column on users
        # to add when that ships). For now we keep the brand promise ("never
        # AI-write") by using a deliberately generic, polite one-line nudge.
        body = _FALLBACK_FOLLOWUP_BODY
        # The picker reuses (user, batch_date, company_id) as a unique key.
        # We're guarded above that no initial uses this company today; safe.
        tbi = TodayBatchItem(
            user_id=sq.user_id,
            batch_date=today,
            company_id=parent_tbi.company_id,
            company_domain=parent_tbi.company_domain,
            to_contact_id=sq.to_contact_id,
            cc_contact_ids=parent_tbi.cc_contact_ids,
            template_id=parent_tbi.template_id,
            subject=_add_reply_subject_prefix(sq.subject),
            body=body,
            send_time=send_time,
            status=followup_status,
            kind="followup",
            parent_send_queue_id=sq.id,
            followup_index=followup_index,
        )
        db.add(tbi)
        summary.planned += 1

    db.commit()
    log.info(
        "followup_planner.plan_complete",
        scanned=summary.scanned,
        planned=summary.planned,
        skipped_by_reason=summary.skipped_by_reason,
    )
    return summary
