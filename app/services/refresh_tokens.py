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
from app.core.time import utcnow
from app.logging_config import get_logger
from app.models import RefreshToken
from app.repositories import refresh_tokens as refresh_tokens_repo

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
      B. Row was already rotated (replaced_by_id set) AND someone is
         presenting it again → REUSE DETECTED. Revoke the whole family.
      C. Row is active (not revoked, not expired, no successor) → ROTATE.
         Mint a new row in the same family, link old → new via
         replaced_by_id, mark old revoked.

    No commit here; caller owns the transaction.
    """
    row = refresh_tokens_repo.get(db, raw_token)
    if row is None:
        return ValidateResult(invalid=True)

    # Reuse detection — present a token that was already swapped out.
    # The legitimate client already moved on to the successor; whoever is
    # holding this is either (a) a stolen copy being replayed, or (b) the
    # legitimate client retrying after a network failure between us setting
    # the new cookie and the client receiving it. Both are indistinguishable
    # without more state, so we conservatively burn the whole family. Net
    # cost in case (b): one re-login. Net cost in case (a): an attacker
    # session is killed.
    if row.replaced_by_id is not None:
        revoked_count = refresh_tokens_repo.revoke_family(db, row.family_id)
        log.warning(
            "refresh.reuse_detected",
            user_id=row.user_id,
            family_id=row.family_id,
            revoked_rows=revoked_count,
        )
        return ValidateResult(reuse_detected=True, user_id=row.user_id)

    if not refresh_tokens_repo.is_active(row):
        # Revoked (logout) or expired past TTL. Quiet 401, no family blast.
        return ValidateResult(invalid=True)

    # Happy path — rotate. New token continues the same family.
    new = issue(
        db,
        user_id=row.user_id,
        family_id=row.family_id,
        user_agent=user_agent,
        ip=ip,
    )
    refresh_tokens_repo.revoke(db, row, replaced_by_id=new.raw_token)
    log.info("refresh.rotated", user_id=row.user_id, family_id=row.family_id)
    return ValidateResult(rotated=new, user_id=row.user_id)


def revoke_family_for_token(db: OrmSession, raw_token: str) -> int:
    """Logout helper: revoke every token in the family that `raw_token` belongs
    to. Idempotent. Returns count of newly-revoked rows; 0 if the token wasn't
    found at all."""
    row = refresh_tokens_repo.get(db, raw_token)
    if row is None:
        return 0
    return refresh_tokens_repo.revoke_family(db, row.family_id)
