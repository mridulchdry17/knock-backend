"""Locks repository — owns SQL for the three Phase 5 lock tables.

Tables managed here:
- `global_contact_lock` — platform-wide 36h cooldown per company domain
- `user_company_locks` — per-user 30-day reply lock per company domain
- `platform_company_locks` — platform-wide permanent stop per company domain

Pure functions; callers own the transaction (no commits inside this module).
All inputs assumed pre-normalized (lowercased domains, tz-aware datetimes).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as OrmSession

from app.core.time import utcnow
from app.models import GlobalContactLock, PlatformCompanyLock, UserCompanyLock

# ─────────────────────────── global_contact_lock (36h cooldown) ───────────────────────────


def get_global_lock(db: OrmSession, company_domain: str) -> GlobalContactLock | None:
    return db.get(GlobalContactLock, company_domain)


def upsert_global_lock(
    db: OrmSession,
    company_domain: str,
    locked_by_user_id: int | None,
    lock_duration_hours: int = 36,
) -> GlobalContactLock:
    """Idempotent upsert. Each call extends `locked_until` to now + duration.

    Used by B5.5's send worker on successful send — rolling cooldown means
    every fresh outbound to @acme.com resets the 36h clock.
    """
    now = utcnow()
    until = now + timedelta(hours=lock_duration_hours)

    row = get_global_lock(db, company_domain)
    if row is None:
        row = GlobalContactLock(
            company_domain=company_domain,
            locked_at=now,
            locked_until=until,
            last_locked_by_user_id=locked_by_user_id,
        )
        db.add(row)
    else:
        row.locked_at = now
        row.locked_until = until
        row.last_locked_by_user_id = locked_by_user_id
        db.add(row)
    db.flush()
    return row


def list_active_global_locks(
    db: OrmSession, *, now: datetime
) -> list[GlobalContactLock]:
    return list(
        db.scalars(
            select(GlobalContactLock)
            .where(GlobalContactLock.locked_until > now)
            .order_by(GlobalContactLock.locked_until.asc())
        ).all()
    )


# ─────────────────────────── user_company_locks (30-day per-user reply) ───────────────────────────


def get_user_company_lock(
    db: OrmSession, user_id: int, company_domain: str
) -> UserCompanyLock | None:
    return db.get(UserCompanyLock, (user_id, company_domain))


def upsert_user_company_lock(
    db: OrmSession,
    user_id: int,
    company_domain: str,
    reason: str,
    duration_days: int = 30,
    is_permanent: bool = False,
) -> UserCompanyLock:
    """Idempotent upsert. Auto-extends on every new reply (rolling 30 days).

    `is_permanent=True` overrides the auto-expire (super_admin escape hatch).
    `locked_until` is still set for consistency, but `is_permanent` takes
    precedence in the service's lock-check logic.
    """
    now = utcnow()
    until = now + timedelta(days=duration_days)

    row = get_user_company_lock(db, user_id, company_domain)
    if row is None:
        row = UserCompanyLock(
            user_id=user_id,
            company_domain=company_domain,
            locked_at=now,
            locked_until=until,
            is_permanent=is_permanent,
            reason=reason,
        )
        db.add(row)
    else:
        row.locked_at = now
        row.locked_until = until
        row.is_permanent = row.is_permanent or is_permanent
        row.reason = reason
        db.add(row)
    db.flush()
    return row


def clear_user_company_lock(
    db: OrmSession, user_id: int, company_domain: str
) -> bool:
    """Returns True if a row was deleted, False if none existed."""
    result = db.execute(
        delete(UserCompanyLock).where(
            UserCompanyLock.user_id == user_id,
            UserCompanyLock.company_domain == company_domain,
        )
    )
    return bool(result.rowcount)


def list_active_user_locks(
    db: OrmSession, user_id: int, *, now: datetime
) -> list[UserCompanyLock]:
    """Active = permanent OR locked_until > now. Sorted by domain."""
    return list(
        db.scalars(
            select(UserCompanyLock)
            .where(UserCompanyLock.user_id == user_id)
            .where(
                (UserCompanyLock.is_permanent.is_(True))
                | (UserCompanyLock.locked_until > now)
            )
            .order_by(UserCompanyLock.company_domain.asc())
        ).all()
    )


# ─────────────────────────── platform_company_locks (permanent stop) ───────────────────────────


def get_platform_lock(
    db: OrmSession, company_domain: str
) -> PlatformCompanyLock | None:
    return db.get(PlatformCompanyLock, company_domain)


def upsert_platform_lock(
    db: OrmSession, company_domain: str, reason: str
) -> PlatformCompanyLock:
    """Idempotent. A second explicit-stop on an already-locked domain is a no-op
    (we keep the original `created_at` as the historic record)."""
    row = get_platform_lock(db, company_domain)
    if row is None:
        row = PlatformCompanyLock(
            company_domain=company_domain,
            reason=reason,
            created_at=utcnow(),
        )
        db.add(row)
        db.flush()
    return row


def clear_platform_lock(db: OrmSession, company_domain: str) -> bool:
    """Returns True if a row was deleted, False if none existed."""
    result = db.execute(
        delete(PlatformCompanyLock).where(
            PlatformCompanyLock.company_domain == company_domain
        )
    )
    return bool(result.rowcount)


def list_platform_locks(db: OrmSession) -> list[PlatformCompanyLock]:
    return list(
        db.scalars(
            select(PlatformCompanyLock).order_by(
                PlatformCompanyLock.created_at.desc()
            )
        ).all()
    )


def list_platform_locks_paginated(
    db: OrmSession, *, limit: int, offset: int
) -> tuple[list[PlatformCompanyLock], int]:
    from sqlalchemy import func

    total = db.scalar(select(func.count()).select_from(PlatformCompanyLock)) or 0
    rows = list(
        db.scalars(
            select(PlatformCompanyLock)
            .order_by(PlatformCompanyLock.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
    return rows, total


def list_global_locks_paginated(
    db: OrmSession, *, now: datetime, limit: int, offset: int
) -> tuple[list[GlobalContactLock], int]:
    """Active-only listing for the admin view."""
    from sqlalchemy import func

    base_count = (
        select(func.count())
        .select_from(GlobalContactLock)
        .where(GlobalContactLock.locked_until > now)
    )
    base_rows = (
        select(GlobalContactLock)
        .where(GlobalContactLock.locked_until > now)
        .order_by(GlobalContactLock.locked_until.asc())
        .limit(limit)
        .offset(offset)
    )
    total = db.scalar(base_count) or 0
    rows = list(db.scalars(base_rows).all())
    return rows, total
