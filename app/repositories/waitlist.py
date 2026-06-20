from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.core.emails import normalize_email
from app.core.time import utcnow
from app.models import WaitlistEntry


def get(db: OrmSession, entry_id: int) -> WaitlistEntry | None:
    return db.get(WaitlistEntry, entry_id)


def get_by_email(db: OrmSession, email: str) -> WaitlistEntry | None:
    return db.scalar(select(WaitlistEntry).where(WaitlistEntry.email == normalize_email(email)))


def exists(db: OrmSession, email: str) -> bool:
    return get_by_email(db, email) is not None


def is_approved(db: OrmSession, email: str) -> bool:
    """True only if the email is on the waitlist AND a super_admin allowed it."""
    entry = get_by_email(db, email)
    return entry is not None and entry.approved_at is not None


def set_approved(
    db: OrmSession,
    entry: WaitlistEntry,
    *,
    approved: bool,
    intended_tier: str = "free",
) -> WaitlistEntry:
    """Allow (approved=True → stamp approved_at=now + record intended tier) or
    revoke (False → NULL). On revoke we reset intended_tier to 'free' so a
    later re-approve doesn't accidentally honour stale 'paid' state. Caller
    commits. Idempotent."""
    if approved:
        entry.approved_at = utcnow()
        entry.intended_tier = intended_tier
    else:
        entry.approved_at = None
        entry.intended_tier = "free"
    db.add(entry)
    db.flush()
    return entry


def add(db: OrmSession, email: str) -> WaitlistEntry:
    entry = WaitlistEntry(email=normalize_email(email))
    db.add(entry)
    db.flush()
    return entry


def add_if_missing(db: OrmSession, email: str) -> tuple[WaitlistEntry, bool]:
    """Idempotent insert. Returns (entry, was_created).
    `was_created=False` means the email was already on the waitlist."""
    existing = get_by_email(db, email)
    if existing is not None:
        return existing, False
    return add(db, email), True


def list_paginated(
    db: OrmSession,
    *,
    limit: int,
    offset: int,
    search: str | None = None,
    status_filter: str = "all",
    sort: str = "newest",
) -> tuple[list[WaitlistEntry], int]:
    """Returns (rows, total).

    Filters:
      - search: case-insensitive substring match on email
      - status_filter: "pending" (approved_at IS NULL), "approved"
        (approved_at IS NOT NULL), or "all"
      - sort: "newest" (default) | "oldest"

    Total reflects the FILTERED query, not the raw table count — pagination
    in the UI works against the post-filter row set."""
    stmt = select(WaitlistEntry)
    count_stmt = select(func.count()).select_from(WaitlistEntry)

    if status_filter == "pending":
        stmt = stmt.where(WaitlistEntry.approved_at.is_(None))
        count_stmt = count_stmt.where(WaitlistEntry.approved_at.is_(None))
    elif status_filter == "approved":
        stmt = stmt.where(WaitlistEntry.approved_at.is_not(None))
        count_stmt = count_stmt.where(WaitlistEntry.approved_at.is_not(None))

    if search:
        # libsql is SQLite-derived → LIKE is case-insensitive on ASCII by
        # default. Lowercasing both sides keeps behaviour identical if we
        # ever move to a case-sensitive collation.
        needle = f"%{search.strip().lower()}%"
        stmt = stmt.where(func.lower(WaitlistEntry.email).like(needle))
        count_stmt = count_stmt.where(func.lower(WaitlistEntry.email).like(needle))

    order_col = (
        WaitlistEntry.created_at.asc()
        if sort == "oldest"
        else WaitlistEntry.created_at.desc()
    )
    stmt = stmt.order_by(order_col).limit(limit).offset(offset)

    rows = list(db.scalars(stmt).all())
    total = db.scalar(count_stmt) or 0
    return rows, total


def approve_many(
    db: OrmSession, entry_ids: list[int]
) -> tuple[int, int, list[int]]:
    """Bulk-approve. Returns (newly_approved, already_approved, not_found_ids).

    Idempotent — passing an already-approved row counts toward
    `already_approved`, not `newly_approved`. Caller commits."""
    newly_approved = 0
    already_approved = 0
    not_found: list[int] = []

    for eid in entry_ids:
        entry = db.get(WaitlistEntry, eid)
        if entry is None:
            not_found.append(eid)
            continue
        if entry.approved_at is not None:
            already_approved += 1
            continue
        entry.approved_at = utcnow()
        db.add(entry)
        newly_approved += 1

    db.flush()
    return newly_approved, already_approved, not_found


def stream_all(db: OrmSession) -> Iterator[WaitlistEntry]:
    """Yields all rows in insertion order. For CSV export — uses .yield_per()
    so we don't load the whole table into memory if it grows large."""
    yield from db.scalars(
        select(WaitlistEntry).order_by(WaitlistEntry.created_at.asc()).execution_options(
            yield_per=500
        )
    )
