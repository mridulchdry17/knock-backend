"""Onboarding endpoints — for users who OAuth'd in but aren't yet approved.

  - GET  /api/v1/onboarding/status         — frontend reads this to decide
                                              dashboard vs onboarding vs
                                              awaiting-approval routing.
  - POST /api/v1/onboarding/claim-waitlist  — user pastes the email they used
                                              on the waitlist (different from
                                              their OAuth email). Maps the
                                              waitlist row to this user.
  - POST /api/v1/onboarding/join-waitlist   — user is brand new; enrolls them
                                              in the waitlist with their
                                              OAuth email and parks tier at
                                              'pending' awaiting super_admin
                                              approval.
"""
from __future__ import annotations

from fastapi import APIRouter, status

from app.core.deps import CurrentUser, DbDep
from app.core.errors import ApiError
from app.logging_config import get_logger
from app.schemas.onboarding import (
    AwaitingApprovalOut,
    ClaimWaitlistIn,
    OnboardingStatusOut,
)
from app.services import onboarding as onboarding_service

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])

log = get_logger("onboarding")


@router.get("/status", response_model=OnboardingStatusOut)
def get_status(user: CurrentUser) -> OnboardingStatusOut:
    return OnboardingStatusOut(
        tier=user.tier,
        waitlist_email=user.waitlist_email,
        onboarded=user.is_onboarded,
    )


@router.post("/claim-waitlist", response_model=OnboardingStatusOut)
def claim_waitlist(
    payload: ClaimWaitlistIn, user: CurrentUser, db: DbDep
) -> OnboardingStatusOut:
    result = onboarding_service.claim_waitlist(db, user, payload.email)
    if result is onboarding_service.ClaimResult.NOT_FOUND:
        raise ApiError(
            "not_on_waitlist",
            "That email isn't on the waitlist. Try the 'I'm new' option to join.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if result is onboarding_service.ClaimResult.TAKEN:
        raise ApiError(
            "waitlist_email_taken",
            "That waitlist email is already linked to another account.",
            status_code=status.HTTP_409_CONFLICT,
        )
    # OK (approved → now free) or PENDING_APPROVAL (linked, still awaiting an
    # admin allow). Both return the user's current status; the frontend routes
    # by tier (free → /today, pending → /awaiting-approval).
    log.info(
        "onboarding.claimed",
        user_id=user.id,
        claimed=payload.email,
        result=result.value,
        tier=user.tier,
    )
    return OnboardingStatusOut(
        tier=user.tier,
        waitlist_email=user.waitlist_email,
        onboarded=user.is_onboarded,
    )


@router.post("/join-waitlist", response_model=AwaitingApprovalOut)
def join_waitlist(user: CurrentUser, db: DbDep) -> AwaitingApprovalOut:
    onboarding_service.join_waitlist(db, user)
    log.info("onboarding.join_ok", user_id=user.id, email=user.email)
    return AwaitingApprovalOut()
