"""Templates repository — SQL for the per-user template library.

Pure functions; callers own the transaction (no commits here). The 3-template
cap is a product rule enforced in the service layer, not here.
"""
from __future__ import annotations

from sqlalchemy import func, select, update
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
    """The template the daily batch + autopilot render with.

    Priority chain:
      1. The row flagged is_default=True (set via the new
         POST /templates/{id}/default endpoint).
      2. Else: the first starter (3 are seeded on first login).
      3. Else: the oldest user-authored template.
      4. Else: None (caller falls back to the hardcoded v0 body).
    """
    return db.scalar(
        select(Template)
        .where(Template.user_id == user_id)
        .order_by(
            Template.is_default.desc(),
            Template.is_starter.desc(),
            Template.created_at.asc(),
        )
        .limit(1)
    )


def set_default(
    db: OrmSession, *, user_id: int, template_id: int
) -> Template | None:
    """Atomically mark one template as the user's default and unset all
    others. Returns the now-default Template, or None if the id doesn't
    belong to the user.

    Two UPDATE statements:
      1. Clear is_default on every OTHER template for this user.
      2. Set is_default=true on the target — only if it's owned by the user
         (the WHERE clause IS the ownership check). rowcount=0 → bogus id,
         return None so the router can 404.
    Caller commits."""
    db.execute(
        update(Template)
        .where(Template.user_id == user_id)
        .where(Template.id != template_id)
        .where(Template.is_default.is_(True))
        .values(is_default=False)
    )
    result = db.execute(
        update(Template)
        .where(Template.id == template_id)
        .where(Template.user_id == user_id)
        .values(is_default=True)
    )
    if int(result.rowcount or 0) == 0:
        return None
    db.flush()
    return db.get(Template, template_id)
