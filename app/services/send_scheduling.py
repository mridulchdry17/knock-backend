"""Send-slot scheduling helpers used outside the picker.

`stamp_late_items_for_user` re-stamps cards whose original send_time has
already passed (e.g. the user reviewed/approved them after their slot fired).
Instead of all such cards becoming immediately-due and blasting out at the
next drain tick, they get queued at the BACK of the user's current schedule
at the tier's cadence (1/hr free, ~1/hr paid) — preserving the spacing the
picker designed.

Worked example (free user, 7 slots 6 AM-12 PM, user approves at 10 AM):
  slots 1-2 (6, 7 AM)  → already sent earlier — untouched
  slots 3-5 (8-10 AM)  → past + still 'default' → late, re-stamp to 1 PM, 2 PM, 3 PM
  slots 6-7 (11, 12 PM)→ future → keep their original times
The latest existing slot is 12 PM; the 3 late items queue after it at 1-hour
cadence.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.core.time import ensure_utc, utcnow
from app.logging_config import get_logger
from app.models import TodayBatchItem, User
from app.services import send_caps
from app.services.today_picker import cadence_for_tier

log = get_logger("send_scheduling")


def stamp_late_items_for_user(
    db: OrmSession,
    user: User,
    late_items: list[TodayBatchItem],
    *,
    now: datetime | None = None,
) -> None:
    """Re-stamp each late item's send_time to the back of the user's existing
    schedule at the tier's cadence. No-op for an empty list. Caller commits.

    "Latest existing" = the max send_time across the user's `ready`/`sent`
    rows today, EXCLUDING the late items themselves (we're about to re-stamp
    those). If there's no future or sent slot to anchor to, we start from
    `now` so the first late item doesn't fire instantly — the scheduler gets
    a full cadence-window to pick it up.
    """
    if not late_items:
        return

    now = now or utcnow()
    today = now.date()
    cap = send_caps.resolve_daily_cap(user)
    cadence = cadence_for_tier(user.tier, cap)  # type: ignore[arg-type]
    late_ids = {i.id for i in late_items if i.id is not None}

    latest_q = (
        select(func.max(TodayBatchItem.send_time))
        .where(TodayBatchItem.user_id == user.id)
        .where(TodayBatchItem.batch_date == today)
        .where(TodayBatchItem.status.in_(("ready", "sent")))
    )
    if late_ids:
        latest_q = latest_q.where(TodayBatchItem.id.notin_(late_ids))

    latest = db.scalar(latest_q)
    anchor = ensure_utc(latest) if latest else now
    if anchor < now:
        # No future slots to follow → start cadence from now so the first late
        # item doesn't dispatch on the next scheduler tick.
        anchor = now

    next_time = anchor + cadence
    for item in late_items:
        item.send_time = next_time
        db.add(item)
        log.info(
            "send_scheduling.late_restamped",
            user_id=user.id,
            today_batch_item_id=item.id,
            new_send_time=next_time.isoformat(),
        )
        next_time = next_time + cadence


def partition_late(
    items: list[TodayBatchItem], *, now: datetime | None = None
) -> tuple[list[TodayBatchItem], list[TodayBatchItem]]:
    """Split items into (late, future) based on send_time vs `now`. Helper for
    callers that need to identify which cards to re-stamp."""
    now = now or utcnow()
    late: list[TodayBatchItem] = []
    future: list[TodayBatchItem] = []
    for it in items:
        if ensure_utc(it.send_time) < now:
            late.append(it)
        else:
            future.append(it)
    return late, future
