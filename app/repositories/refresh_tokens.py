"""Refresh token CRUD. Compound logic (issue/validate/rotate/revoke_family)
lives in `app.services.refresh_tokens`; this module is the SQL-only layer."""
from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session as OrmSession

from app.core.time import ensure_utc, utcnow
from app.models import RefreshToken


def get(db: OrmSession, token: str) -> RefreshToken | None:
    """Return the row regardless of revoked/expired state — callers need
    to distinguish 'never existed' (None) from 'was valid, now revoked'
    (row.revoked_at is not None) to detect token reuse."""
    return db.get(RefreshToken, token)


def add(db: OrmSession, row: RefreshToken) -> RefreshToken:
    db.add(row)
    db.flush()
    return row


def is_active(row: RefreshToken) -> bool:
    """True iff the token is usable: not revoked, not expired, no successor."""
    if row.revoked_at is not None:
        return False
    if ensure_utc(row.expires_at) <= utcnow():
        return False
    if row.replaced_by_id is not None:
        return False
    return True


def revoke(db: OrmSession, row: RefreshToken, *, replaced_by_id: str | None = None) -> None:
    """Mark this single row revoked. `replaced_by_id` links the chain when
    revocation is due to rotation; left None when revocation is due to logout
    or family invalidation."""
    row.revoked_at = utcnow()
    if replaced_by_id is not None:
        row.replaced_by_id = replaced_by_id
    db.add(row)


def revoke_family(db: OrmSession, family_id: str) -> int:
    """Whole-family revocation — used when reuse is detected. Returns the
    count of newly-revoked rows (already-revoked rows are not double-stamped).
    Idempotent."""
    result = db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id)
        .where(RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    return int(result.rowcount or 0)
