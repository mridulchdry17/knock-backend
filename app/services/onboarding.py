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
    OK = "ok"
    NOT_FOUND = "not_found"
    TAKEN = "taken"


def claim_waitlist(db: OrmSession, user: User, claimed_email: str) -> ClaimResult:
    """User asserts they signed up to the waitlist with a different email earlier.

      - Email not on waitlist          → NOT_FOUND.
      - Email on waitlist, free        → set users.waitlist_email + tier='free'. OK.
      - Email on waitlist, but already claimed by another user → TAKEN.
      - User re-claims their own already-claimed email → OK (idempotent).
    """
    target = normalize_email(claimed_email)

    if not waitlist_repo.exists(db, target):
        return ClaimResult.NOT_FOUND

    existing_holder = users_repo.get_by_waitlist_email(db, target)
    if existing_holder is not None and existing_holder.id != user.id:
        return ClaimResult.TAKEN

    users_repo.set_waitlist_email(db, user, target)
    users_repo.set_tier(db, user, "free")
    db.commit()
    return ClaimResult.OK


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
