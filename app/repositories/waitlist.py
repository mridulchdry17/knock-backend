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


def set_approved(db: OrmSession, entry: WaitlistEntry, *, approved: bool) -> WaitlistEntry:
    """Allow (approved=True → stamp approved_at=now) or revoke (False → NULL).
    Caller commits. Idempotent."""
    entry.approved_at = utcnow() if approved else None
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
    db: OrmSession, *, limit: int, offset: int
) -> tuple[list[WaitlistEntry], int]:
    """Returns (rows, total). Newest first."""
    rows = list(
        db.scalars(
            select(WaitlistEntry)
            .order_by(WaitlistEntry.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
    total = db.scalar(select(func.count()).select_from(WaitlistEntry)) or 0
    return rows, total


def stream_all(db: OrmSession) -> Iterator[WaitlistEntry]:
    """Yields all rows in insertion order. For CSV export — uses .yield_per()
    so we don't load the whole table into memory if it grows large."""
    yield from db.scalars(
        select(WaitlistEntry).order_by(WaitlistEntry.created_at.asc()).execution_options(
            yield_per=500
        )
    )
