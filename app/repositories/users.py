from __future__ import annotations

from sqlalchemy import func, or_, select
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


def list_paginated(
    db: OrmSession,
    *,
    tier: str | None = None,
    search: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[User], int]:
    """Returns (rows, total). `search` matches email or full_name (case-insensitive,
    substring). `tier` is an exact match. Both filters are optional and combinable.
    """
    base = select(User)
    count_base = select(func.count()).select_from(User)

    if tier is not None:
        base = base.where(User.tier == tier)
        count_base = count_base.where(User.tier == tier)

    if search:
        needle = f"%{search.lower()}%"
        cond = or_(
            func.lower(User.email).like(needle),
            func.lower(User.full_name).like(needle),
        )
        base = base.where(cond)
        count_base = count_base.where(cond)

    rows = list(
        db.scalars(base.order_by(User.created_at.desc()).limit(limit).offset(offset)).all()
    )
    total = db.scalar(count_base) or 0
    return rows, total
