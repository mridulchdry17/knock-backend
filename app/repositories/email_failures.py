"""email_failures repository — owns SQL for the send-failure dashboard table.

Caller-owns-txn (no commits inside).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.models import EmailFailure

_MAX_ERROR_LEN = 2000


def add(
    db: OrmSession,
    *,
    user_id: int,
    today_batch_item_id: int | None,
    company_domain: str,
    failure_kind: str,
    error_message: str,
    gmail_error_code: str | None,
) -> EmailFailure:
    """Truncates `error_message` to 2000 chars before insert."""
    row = EmailFailure(
        user_id=user_id,
        today_batch_item_id=today_batch_item_id,
        company_domain=company_domain,
        failure_kind=failure_kind,
        error_message=(error_message or "")[:_MAX_ERROR_LEN],
        gmail_error_code=gmail_error_code,
    )
    db.add(row)
    db.flush()
    return row


def list_recent(
    db: OrmSession,
    *,
    limit: int = 50,
    offset: int = 0,
    user_id: int | None = None,
    failure_kind: str | None = None,
) -> tuple[list[EmailFailure], int]:
    """Newest-first listing with optional user_id / failure_kind filters."""
    base = select(EmailFailure)
    count_base = select(func.count()).select_from(EmailFailure)

    if user_id is not None:
        base = base.where(EmailFailure.user_id == user_id)
        count_base = count_base.where(EmailFailure.user_id == user_id)
    if failure_kind is not None:
        base = base.where(EmailFailure.failure_kind == failure_kind)
        count_base = count_base.where(EmailFailure.failure_kind == failure_kind)

    rows = list(
        db.scalars(
            base.order_by(EmailFailure.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
    total = db.scalar(count_base) or 0
    return rows, total


def count_by_kind(db: OrmSession, *, since: datetime) -> dict[str, int]:
    """Aggregate failure counts grouped by `failure_kind` for rows >= `since`.

    Returns a dict (possibly empty). Kinds not present in the window are absent
    from the result; callers can default-zero them at the schema boundary.
    """
    rows = db.execute(
        select(EmailFailure.failure_kind, func.count())
        .where(EmailFailure.created_at >= since)
        .group_by(EmailFailure.failure_kind)
    ).all()
    return {kind: count for kind, count in rows}
