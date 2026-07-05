"""Autopilot stop-condition evaluation.

Called by the autopilot cycle BEFORE per-user batch generation. Returns
whether autopilot should pause for that user right now, and if so, why —
the reason string is persisted on `users.autopilot_paused_reason` and
surfaced to the frontend so we can render "paused because you hit N
replies", "paused at your end date", etc.

## Evaluation order

Platform ceilings ALWAYS win over user-selected conditions. This matters
when both would fire in the same cycle (e.g. user picked 'replies'=5 but
they've also sent 500 total): the ceiling reason ('ceiling_sends' /
'ceiling_days') gets persisted, not the user's condition. That way ops
can distinguish system-imposed pauses from user-configured ones.

## Anchor

Every counter (sends, replies, days) is measured from
`user.autopilot_enabled_at`. That column is set on every autopilot toggle-on
(and preserved on toggle-off), so a user who pauses and re-enables gets a
fresh counter window.

If `autopilot_enabled_at IS NULL` (a freshly-migrated user who was NOT on
autopilot before): treat all counters as zero. Never crash.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.core.time import ensure_utc, utcnow
from app.models import SendQueue, User

# ─────────────────────────── platform ceilings ───────────────────────────
# Hard caps that override any user choice. See project_phase5_send_model.md
# and the stop-conditions spec — user explicitly dropped the reply ceiling,
# only sends + days remain.
CEILING_MAX_SENDS: int = 500
CEILING_MAX_DAYS: int = 90


# ─────────────────────────── counter helpers ───────────────────────────


def _sends_since_enabled(user: User, db: OrmSession) -> int:
    """Count sends attributed to autopilot's current window.

    `send_queue.status='SENT'` is the cleaner source of truth here (the
    send worker writes it; email_logs is append-only but broader — includes
    ingest / manual events). Uses `sent_at`, not `created_at`, so pre-queued
    but unsent rows don't count.
    """
    if user.autopilot_enabled_at is None:
        return 0
    anchor = ensure_utc(user.autopilot_enabled_at)
    result = db.scalar(
        select(func.count())
        .select_from(SendQueue)
        .where(SendQueue.user_id == user.id)
        .where(SendQueue.status == "SENT")
        .where(SendQueue.sent_at.is_not(None))
        .where(SendQueue.sent_at >= anchor)
    )
    return int(result or 0)


def _replies_since_enabled(user: User, db: OrmSession) -> int:
    """Count inbound replies received during autopilot's current window.

    Replies are stamped by the reply ingestor as status='REPLIED' with the
    matched thread's `replied_at`. That's what the frontend renders in the
    inbox and what the follow-up planner already keys on — reuse it.
    """
    if user.autopilot_enabled_at is None:
        return 0
    anchor = ensure_utc(user.autopilot_enabled_at)
    result = db.scalar(
        select(func.count())
        .select_from(SendQueue)
        .where(SendQueue.user_id == user.id)
        .where(SendQueue.status == "REPLIED")
        .where(SendQueue.replied_at.is_not(None))
        .where(SendQueue.replied_at >= anchor)
    )
    return int(result or 0)


def _days_since_enabled(user: User, now: datetime) -> int:
    """Whole-day count from enabled_at → now. Same-day returns 0."""
    if user.autopilot_enabled_at is None:
        return 0
    anchor = ensure_utc(user.autopilot_enabled_at)
    return (now.date() - anchor.date()).days


# ─────────────────────────── public API ───────────────────────────


def should_pause(
    user: User,
    db: OrmSession,
    *,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    """Return (should_pause, reason).

    Reason ∈ {'ceiling_sends', 'ceiling_days', 'replies', 'end_date',
              'budget', None}.

    Called per-user in the autopilot cycle before batch generation. Platform
    ceilings evaluate first, then the user's chosen condition. Returns
    (False, None) when the user has nothing configured (stop_type='none')
    and no ceiling is hit — the common case.
    """
    now = now or utcnow()

    # 1. Platform ceilings — always evaluated, regardless of user choice.
    sends = _sends_since_enabled(user, db)
    if sends >= CEILING_MAX_SENDS:
        return True, "ceiling_sends"

    days = _days_since_enabled(user, now)
    if days >= CEILING_MAX_DAYS:
        return True, "ceiling_days"

    # 2. User's chosen condition.
    stop_type = user.autopilot_stop_type or "none"

    if stop_type == "replies":
        threshold = user.autopilot_stop_at_replies
        if threshold is not None:
            replies = _replies_since_enabled(user, db)
            if replies >= threshold:
                return True, "replies"

    elif stop_type == "end_date":
        # today() >= chosen date. Same-day fires (rather than only *after*)
        # so a user who picks tomorrow gets exactly one more day of sends.
        target = user.autopilot_stop_at_date
        if target is not None and now.date() >= target:
            return True, "end_date"

    elif stop_type == "budget":
        # Reuses the sends count already computed for the ceiling check —
        # no extra query. Budget always <= ceiling (200 vs 500) so ordering
        # naturally lets the ceiling win when both would fire on the same
        # tick.
        threshold = user.autopilot_stop_at_budget
        if threshold is not None and sends >= threshold:
            return True, "budget"

    return False, None
