"""Refresh token CRUD. Compound logic (issue/validate/rotate/revoke_family)
lives in `app.services.refresh_tokens`; this module is the SQL-only layer."""
from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session as OrmSession

from app.core.time import ensure_utc, utcnow
from app.models import RefreshToken


def claim_for_rotation(
    db: OrmSession,
    *,
    raw_token: str,
    new_token_id: str,
) -> bool:
    """Atomically mark a token as rotated. Returns True iff the row was
    matched and updated (rowcount == 1) — i.e. WE won the race and own the
    rotation. Returns False when a concurrent rotation got there first
    (rowcount == 0).

    The single UPDATE statement is the critical section: it only succeeds
    against a row that is still un-revoked and un-replaced. libsql/SQLite
    atomicity per statement is sufficient — we never have to hold a lock
    across multiple round-trips.

    Expiry isn't included in the WHERE clause because libsql strips tzinfo
    on storage (so the stored expires_at is naive while utcnow() is aware,
    and SQLAlchemy's evaluator can't compare them). Callers gate on
    `is_active(row)` before claim, which does the expiry check with
    `ensure_utc()`. The race window between is_active and the UPDATE is
    sub-millisecond; if a row expires in that window we'd rotate a hair
    late, but the new row gets a fresh 30-day TTL so no harm done."""
    result = db.execute(
        update(RefreshToken)
        .where(RefreshToken.id == raw_token)
        .where(RefreshToken.replaced_by_id.is_(None))
        .where(RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow(), replaced_by_id=new_token_id)
    )
    return int(result.rowcount or 0) == 1


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
    return row.replaced_by_id is None


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


def revoke_all_for_user(db: OrmSession, user_id: int) -> int:
    """Nuke every active refresh token for a user — used by /disconnect
    (force re-auth on every device). Returns count of newly-revoked rows."""
    result = db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id)
        .where(RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    return int(result.rowcount or 0)
