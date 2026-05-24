"""Onboarding business logic.

Handles the two onboarding paths a user can take after signing up via OAuth
without a waitlist match:

  1. They tell us they used a different email on the waitlist → claim that.
  2. They're new and want to join the waitlist → enroll them, mark pending.

Routers translate the result enum into HTTP responses; this module owns the
DB rules.
"""
from __future__ import annotations

from enum import StrEnum

from sqlalchemy.orm import Session as OrmSession

from app.core.emails import normalize_email
from app.models import User
from app.repositories import users as users_repo
from app.repositories import waitlist as waitlist_repo


class ClaimResult(StrEnum):
    OK = "ok"  # email is on the waitlist AND approved → user is now 'free'
    PENDING_APPROVAL = "pending_approval"  # on the waitlist, not allowed in yet
    NOT_FOUND = "not_found"
    TAKEN = "taken"


def claim_waitlist(db: OrmSession, user: User, claimed_email: str) -> ClaimResult:
    """User asserts they signed up to the waitlist with a different email earlier.

      - Email not on waitlist                       → NOT_FOUND.
      - Email already claimed by another user       → TAKEN.
      - Email on waitlist + approved                → link it + tier='free'. OK.
      - Email on waitlist but NOT approved yet       → link it, tier='pending'.
                                                       PENDING_APPROVAL.
      - User re-claims their own email              → idempotent (OK or
                                                       PENDING_APPROVAL by status).

    Claiming always *links* the waitlist row to this account (so the admin can
    find and approve them), but access is only granted if the entry is approved.
    """
    target = normalize_email(claimed_email)

    entry = waitlist_repo.get_by_email(db, target)
    if entry is None:
        return ClaimResult.NOT_FOUND

    existing_holder = users_repo.get_by_waitlist_email(db, target)
    if existing_holder is not None and existing_holder.id != user.id:
        return ClaimResult.TAKEN

    users_repo.set_waitlist_email(db, user, target)

    if entry.approved_at is not None:
        users_repo.set_tier(db, user, "free")
        _seed_starters_safely(db, user)
        db.commit()
        return ClaimResult.OK

    # On the list, but a super_admin hasn't allowed this entry in yet.
    users_repo.set_tier(db, user, "pending")
    db.commit()
    return ClaimResult.PENDING_APPROVAL


def _seed_starters_safely(db: OrmSession, user: User) -> None:
    """Seed the 3 starter templates when a user reaches an active tier. Lazy
    import keeps onboarding light and avoids a circular import with templates."""
    from app.services import templates as templates_svc

    templates_svc.seed_starters(db, user)


def join_waitlist(db: OrmSession, user: User) -> None:
    """User declares they're new — enroll their OAuth email in the waitlist
    and mark them as awaiting approval.

    Idempotent: re-running for the same user is a no-op.
    """
    waitlist_repo.add_if_missing(db, user.email)

    if user.waitlist_email != user.email:
        users_repo.set_waitlist_email(db, user, user.email)

    # Tier stays 'pending' — super_admin must approve.
    users_repo.set_tier(db, user, "pending")
    db.commit()
