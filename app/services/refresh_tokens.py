"""Issue / validate / rotate refresh tokens with family-based reuse detection.

The pattern (industry-standard, e.g. Auth0, Stripe):

    Login        ──▶  fresh family + first refresh token
    Refresh      ──▶  rotate: revoke current, mint successor in same family
    Reuse seen   ──▶  REVOKE WHOLE FAMILY. legitimate device + attacker both
                      forced to re-login on next request.

`validate_and_rotate` is the central operation: it's called by the refresh
endpoint on every silent-refresh hit. The reuse path is the security pivot —
without it, a token leak is permanent; with it, a leak self-detects on the
attacker's second use.

Caller owns the commit boundary; this module never commits.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session as OrmSession

from app.config import settings
from app.core.time import ensure_utc, utcnow
from app.logging_config import get_logger
from app.models import RefreshToken
from app.repositories import refresh_tokens as refresh_tokens_repo

# Network-retry grace: if a token whose `replaced_by_id` is set is presented
# again WITHIN this window after the rotation, treat it as a network retry
# (the legitimate client's first refresh response was lost in flight) and
# return the existing successor. Outside the window, treat as reuse and burn
# the family.
#
# 30 seconds is generous enough for any reasonable TCP retransmit on mobile
# networks while still being tight enough that a stolen-cookie replay is
# almost certainly past the window by the time an attacker uses it.
_NETWORK_RETRY_GRACE = timedelta(seconds=30)

log = get_logger("refresh_tokens")

# 32 bytes → 43-char urlsafe-base64. Same width as the access token.
_TOKEN_BYTES = 32


def _generate_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _generate_family_id() -> str:
    # Family id is itself a random secret. Not strictly required (the token is
    # the only thing that needs to be unguessable) but a 32-byte family id
    # gives us a fully-opaque key for the rotation chain — no risk of a
    # numeric autoincrement being enumerable on a future admin endpoint.
    return secrets.token_urlsafe(_TOKEN_BYTES)


@dataclass(frozen=True, slots=True)
class IssueResult:
    """Returned to the caller after a fresh login or a rotation."""

    raw_token: str
    family_id: str
    expires_at_iso: str


def issue(
    db: OrmSession,
    *,
    user_id: int,
    family_id: str | None = None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> IssueResult:
    """Mint a new refresh token. Pass `family_id` to continue an existing
    rotation chain; omit it (None) to start a fresh family — i.e. a fresh
    login event."""
    fam = family_id or _generate_family_id()
    raw = _generate_token()
    expires = utcnow() + timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)
    row = RefreshToken(
        id=raw,
        user_id=user_id,
        family_id=fam,
        expires_at=expires,
        user_agent=user_agent,
        ip=ip,
    )
    refresh_tokens_repo.add(db, row)
    return IssueResult(raw_token=raw, family_id=fam, expires_at_iso=expires.isoformat())


@dataclass(frozen=True, slots=True)
class ValidateResult:
    """Outcome of validate_and_rotate. Exactly one of `rotated`, `reuse_detected`,
    or `invalid` will be truthy."""

    rotated: IssueResult | None = None
    """Set when the presented token was valid and we minted a successor.
    Caller should write the new token back to the browser as a Set-Cookie."""

    reuse_detected: bool = False
    """Set when the presented token was already-replaced (suspected theft).
    The whole family has been revoked. Caller should clear the cookie and
    return 401 — user must re-login."""

    invalid: bool = False
    """Set when the token doesn't exist, is expired past TTL, or was revoked
    via logout. Caller should clear the cookie and return 401, but this does
    NOT signal suspected reuse — no family-wide blast."""

    user_id: int | None = None
    """Populated on rotated + on reuse_detected, so the caller can log
    structured events tied to the user."""


def validate_and_rotate(
    db: OrmSession,
    *,
    raw_token: str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> ValidateResult:
    """Look up `raw_token`, classify, and (in the happy path) rotate.

    Cases:
      A. Row not found / expired past TTL / revoked-with-no-successor →
         INVALID (caller: 401, clear cookie).
      B. Row was already rotated (replaced_by_id set) AND within the network-
         retry grace window → RETURN THE EXISTING SUCCESSOR. This handles the
         legitimate-client failure mode where the response to the first
         refresh was lost in flight; the client retries with the same cookie,
         and we hand back the same successor instead of burning everything.
      C. Row was already rotated AND past the grace window → REUSE DETECTED.
         Revoke the whole family.
      D. Row is active → atomic CAS rotation via claim_for_rotation. If we
         win, insert the new row and return it. If we lose to a concurrent
         caller, fall through to case B (which now picks up the successor
         the winner inserted).

    No commit inside the lock-claim path; caller owns the transaction.
    """
    row = refresh_tokens_repo.get(db, raw_token)
    if row is None:
        return ValidateResult(invalid=True)

    # Case B/C: token has already been rotated. Decide between network-retry
    # grace and reuse-detection based on how long ago the rotation happened.
    if row.replaced_by_id is not None:
        return _handle_already_rotated(db, row)

    if not refresh_tokens_repo.is_active(row):
        # Revoked (logout) or expired past TTL with no successor. Quiet 401.
        return ValidateResult(invalid=True)

    # Case D: claim the rotation atomically. Pre-generate the successor's
    # raw_token so we can fold it into the UPDATE without a follow-up read.
    new_raw_token = _generate_token()
    won_race = refresh_tokens_repo.claim_for_rotation(
        db, raw_token=raw_token, new_token_id=new_raw_token
    )

    if not won_race:
        # Concurrent rotation got there first. Re-read the row to find their
        # successor, and return it as our result so the client ends up with
        # the same successor cookie regardless of which call won.
        db.expire(row)
        return _handle_already_rotated(db, refresh_tokens_repo.get(db, raw_token))

    # We won — persist the successor row and return it.
    expires = utcnow() + timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)
    successor_row = RefreshToken(
        id=new_raw_token,
        user_id=row.user_id,
        family_id=row.family_id,
        expires_at=expires,
        user_agent=user_agent,
        ip=ip,
    )
    refresh_tokens_repo.add(db, successor_row)
    log.info("refresh.rotated", user_id=row.user_id, family_id=row.family_id)
    return ValidateResult(
        rotated=IssueResult(
            raw_token=new_raw_token,
            family_id=row.family_id,
            expires_at_iso=expires.isoformat(),
        ),
        user_id=row.user_id,
    )


def _handle_already_rotated(
    db: OrmSession, row: RefreshToken | None
) -> ValidateResult:
    """Shared logic for case B (network-retry grace) and case C (reuse)."""
    if row is None or row.replaced_by_id is None:
        # Race + expire_all means we may not see the rotation any more, OR
        # the row was nuked by a family-revoke between our two reads. Either
        # way: invalid, no family blast.
        return ValidateResult(invalid=True)

    rotated_at = ensure_utc(row.revoked_at) if row.revoked_at else None
    successor = refresh_tokens_repo.get(db, row.replaced_by_id)

    within_grace = (
        rotated_at is not None
        and (utcnow() - rotated_at) <= _NETWORK_RETRY_GRACE
        and successor is not None
        and refresh_tokens_repo.is_active(successor)
    )
    if within_grace:
        # Network-retry: hand back the existing successor without burning
        # anything. The client ends up with the same cookie they would have
        # had on the first (lost-in-flight) response.
        log.info(
            "refresh.network_retry_replay",
            user_id=row.user_id,
            family_id=row.family_id,
        )
        # successor is guaranteed non-None inside `within_grace`.
        assert successor is not None
        return ValidateResult(
            rotated=IssueResult(
                raw_token=successor.id,
                family_id=successor.family_id,
                expires_at_iso=ensure_utc(successor.expires_at).isoformat(),
            ),
            user_id=row.user_id,
        )

    # Past grace window or successor already invalidated → genuine reuse.
    revoked_count = refresh_tokens_repo.revoke_family(db, row.family_id)
    log.warning(
        "refresh.reuse_detected",
        user_id=row.user_id,
        family_id=row.family_id,
        revoked_rows=revoked_count,
    )
    return ValidateResult(reuse_detected=True, user_id=row.user_id)


def revoke_family_for_token(db: OrmSession, raw_token: str) -> int:
    """Logout helper: revoke every token in the family that `raw_token` belongs
    to. Idempotent. Returns count of newly-revoked rows; 0 if the token wasn't
    found at all."""
    row = refresh_tokens_repo.get(db, raw_token)
    if row is None:
        return 0
    return refresh_tokens_repo.revoke_family(db, row.family_id)
