from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.core.emails import normalize_email
from app.core.time import utcnow
from app.models import User


def get(db: OrmSession, user_id: int) -> User | None:
    return db.get(User, user_id)


def get_by_email(db: OrmSession, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == normalize_email(email)))


def get_by_google_sub(db: OrmSession, sub: str) -> User | None:
    return db.scalar(select(User).where(User.google_sub == sub))


def get_by_waitlist_email(db: OrmSession, email: str) -> User | None:
    """Used to detect double-claims of the same waitlist row."""
    return db.scalar(select(User).where(User.waitlist_email == normalize_email(email)))


def add(db: OrmSession, user: User) -> User:
    db.add(user)
    db.flush()
    return user


def set_tier(db: OrmSession, user: User, tier: str) -> None:
    """Update tier + tier_set_at. No-op if already at that tier."""
    if user.tier == tier:
        return
    user.tier = tier
    user.tier_set_at = utcnow()
    db.add(user)


def set_waitlist_email(db: OrmSession, user: User, email: str) -> None:
    """Caller must ensure no other user holds this waitlist_email
    (use get_by_waitlist_email first). Caller is also responsible for tier
    changes — this function only sets the field."""
    user.waitlist_email = normalize_email(email)
    db.add(user)
