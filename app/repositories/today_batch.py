"""today_batch_items repository — owns SQL for the per-user daily batch.

Pure functions; callers own commits.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.models import TodayBatchItem


def has_batch_for_date(db: OrmSession, user_id: int, batch_date: date) -> bool:
    """Used by the cron service for the idempotent re-run check."""
    count = db.scalar(
        select(func.count())
        .select_from(TodayBatchItem)
        .where(TodayBatchItem.user_id == user_id)
        .where(TodayBatchItem.batch_date == batch_date)
    )
    return bool(count)


def list_for_user_date(
    db: OrmSession, user_id: int, batch_date: date
) -> list[TodayBatchItem]:
    """Backs GET /today. Ordered by send_time so the UI renders the day in
    chronological order."""
    return list(
        db.scalars(
            select(TodayBatchItem)
            .where(TodayBatchItem.user_id == user_id)
            .where(TodayBatchItem.batch_date == batch_date)
            .order_by(TodayBatchItem.send_time.asc(), TodayBatchItem.id.asc())
        ).all()
    )


def get(db: OrmSession, item_id: int) -> TodayBatchItem | None:
    return db.get(TodayBatchItem, item_id)


def add(db: OrmSession, item: TodayBatchItem) -> TodayBatchItem:
    db.add(item)
    db.flush()
    return item


def update_status(db: OrmSession, item_id: int, status: str) -> None:
    """Used by the send worker (B5.5) to mark cards 'sent' / 'cooldown' / etc."""
    row = db.get(TodayBatchItem, item_id)
    if row is None:
        return
    row.status = status
    db.add(row)


def list_users_with_batches_on_date(
    db: OrmSession, batch_date: date
) -> list[int]:
    """Admin/observability: which users have already had a batch generated today."""
    rows = db.execute(
        select(TodayBatchItem.user_id)
        .where(TodayBatchItem.batch_date == batch_date)
        .group_by(TodayBatchItem.user_id)
    ).all()
    return [int(r[0]) for r in rows]
