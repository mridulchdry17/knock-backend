from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.core.emails import normalize_email
from app.models import WaitlistEntry


def get_by_email(db: OrmSession, email: str) -> WaitlistEntry | None:
    return db.scalar(select(WaitlistEntry).where(WaitlistEntry.email == normalize_email(email)))


def exists(db: OrmSession, email: str) -> bool:
    return get_by_email(db, email) is not None


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
