"""Auth orchestration.

`complete_google_login` is the single entry point used by the OAuth callback
router. It owns the multi-step orchestration:

  1. Upsert the user from the Google identity.
  2. Apply the tier decision tree (super-admin allowlist, waitlist auto-claim).
  3. Issue a session.

Routers should call this and never touch the DB / crypto / tier logic
themselves. This keeps HTTP concerns in the router and persistence + business
rules here.

The tier decision tree is split into a pure-ish function (`decide_tier_and_destination`)
so it's unit-testable without an HTTP framework or a real Google identity.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session as OrmSession

from app.config import settings
from app.core.crypto import encrypt
from app.core.emails import normalize_email
from app.core.time import utcnow
from app.models import Session as SessionRow
from app.models import User
from app.repositories import users as users_repo
from app.repositories import waitlist as waitlist_repo
from app.services import sessions as sessions_service
from app.services.google_oauth import GoogleIdentity

# ─────────────────────────── tier decision ───────────────────────────


@dataclass(frozen=True, slots=True)
class TierDecision:
    """The outcome of the tier decision tree for a single OAuth callback.

    `next_path` is what the frontend should land the user on after consuming
    the bearer token. `claim_email` is non-None only when we auto-claimed a
    waitlist row (so the orchestrator knows to set `users.waitlist_email`).
    """

    new_tier: str
    next_path: str
    claim_email: str | None  # None unless we auto-matched OAuth email to a waitlist row


def _is_super_admin_email(email: str) -> bool:
    return normalize_email(email) in settings.super_admin_emails_set


def decide_tier_and_destination(db: OrmSession, user: User) -> TierDecision:
    """Pure-ish: reads from db (waitlist + users), but does not mutate.

    Decision tree (in order):
      1. Email in SUPER_ADMIN_EMAILS → super_admin → /today.
      2. Already onboarded (waitlist_email IS NOT NULL) and tier != pending →
         keep tier, /today.
      3. Already onboarded but tier == pending → still awaiting approval →
         frontend routes them to /awaiting-approval based on /onboarding/status.
      4. Not yet onboarded but OAuth email is on the waitlist → auto-claim,
         tier='free', /today.
      5. Not yet onboarded and no waitlist match → leave tier='pending',
         /onboarding (frontend will offer claim-other-email or join-waitlist).
    """
    if _is_super_admin_email(user.email):
        return TierDecision(new_tier="super_admin", next_path="/today", claim_email=None)

    if user.waitlist_email is not None:
        # Returning user — keep whatever tier they already have. Do not
        # silently re-promote a pending user; admin must approve them.
        if user.tier == "pending":
            return TierDecision(new_tier="pending", next_path="/onboarding", claim_email=None)
        return TierDecision(new_tier=user.tier, next_path="/today", claim_email=None)

    # Not onboarded yet. Try waitlist auto-claim.
    if waitlist_repo.exists(db, user.email):
        return TierDecision(new_tier="free", next_path="/today", claim_email=user.email)

    return TierDecision(new_tier="pending", next_path="/onboarding", claim_email=None)


# ─────────────────────────── user upsert + login ───────────────────────────


def _upsert_user(db: OrmSession, identity: GoogleIdentity) -> User:
    user = users_repo.get_by_google_sub(db, identity.sub)
    if user is None:
        user = users_repo.get_by_email(db, identity.email)
    if user is None:
        user = users_repo.add(
            db,
            User(
                email=normalize_email(identity.email),
                full_name=identity.full_name,
                google_sub=identity.sub,
                # tier defaults to 'pending' from the migration server_default;
                # decide_tier_and_destination upgrades this in the same request.
            ),
        )

    user.email = normalize_email(identity.email)
    user.full_name = identity.full_name or user.full_name
    user.google_sub = identity.sub
    user.google_refresh_token = encrypt(identity.refresh_token)
    user.google_access_token = encrypt(identity.access_token)
    user.google_token_expiry = identity.token_expiry
    user.google_scopes = " ".join(sorted(identity.granted_scopes))
    user.google_connected_at = utcnow()
    db.flush()
    return user


def complete_google_login(
    db: OrmSession,
    identity: GoogleIdentity,
    *,
    user_agent: str | None,
    ip: str | None,
) -> tuple[User, SessionRow, TierDecision]:
    """End-to-end OAuth callback orchestration. Returns the (user, session,
    decision) tuple so the router can build the redirect URL.

    Owns the commit boundary."""
    user = _upsert_user(db, identity)
    decision = decide_tier_and_destination(db, user)

    # Apply mutations from the decision tree.
    if decision.claim_email is not None:
        users_repo.set_waitlist_email(db, user, decision.claim_email)
    users_repo.set_tier(db, user, decision.new_tier)

    # Seed the 3 starter templates once the user reaches an active tier.
    # Idempotent (no-op if they already have any), so it's safe on every login;
    # pending users don't get them until approved.
    if decision.new_tier != "pending":
        from app.services import templates as templates_svc

        templates_svc.seed_starters(db, user)

    session = sessions_service.issue(db, user_id=user.id, user_agent=user_agent, ip=ip)
    db.commit()
    return user, session, decision
