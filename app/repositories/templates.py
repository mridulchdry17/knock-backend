"""Templates repository — SQL for the per-user template library.

Pure functions; callers own the transaction (no commits here). The 3-template
cap is a product rule enforced in the service layer, not here.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.models import Template, TodayBatchItem


def list_for_user(db: OrmSession, user_id: int) -> list[Template]:
    """All of a user's templates, starters first then newest — stable order
    for the library UI."""
    return list(
        db.scalars(
            select(Template)
            .where(Template.user_id == user_id)
            .order_by(Template.is_starter.desc(), Template.created_at.asc())
        ).all()
    )


def get(db: OrmSession, template_id: int) -> Template | None:
    return db.get(Template, template_id)


def count_for_user(db: OrmSession, user_id: int) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Template)
            .where(Template.user_id == user_id)
        )
        or 0
    )


def add(db: OrmSession, template: Template) -> Template:
    db.add(template)
    db.flush()
    return template


def delete(db: OrmSession, template: Template) -> None:
    db.delete(template)
    db.flush()


def used_counts_for_user(db: OrmSession, user_id: int) -> dict[int, int]:
    """Map template_id → number of today_batch_items that referenced it, for
    this user. One grouped query backs the whole list (no per-row N+1)."""
    rows = db.execute(
        select(TodayBatchItem.template_id, func.count())
        .where(TodayBatchItem.user_id == user_id)
        .where(TodayBatchItem.template_id.is_not(None))
        .group_by(TodayBatchItem.template_id)
    ).all()
    return {int(tid): int(n) for tid, n in rows if tid is not None}


def default_for_user(db: OrmSession, user_id: int) -> Template | None:
    """The template the daily batch renders with when the user hasn't picked
    one per-card: their first starter, else their oldest template, else None.
    Implicit-default model — there's no set-default UI in v0."""
    return db.scalar(
        select(Template)
        .where(Template.user_id == user_id)
        .order_by(Template.is_starter.desc(), Template.created_at.asc())
        .limit(1)
    )
