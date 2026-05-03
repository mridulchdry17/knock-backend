from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.models import WaitlistEntry


def get_by_email(db: OrmSession, email: str) -> WaitlistEntry | None:
    return db.scalar(select(WaitlistEntry).where(WaitlistEntry.email == email.lower()))


def add(db: OrmSession, email: str) -> WaitlistEntry:
    entry = WaitlistEntry(email=email.lower())
    db.add(entry)
    db.flush()
    return entry
