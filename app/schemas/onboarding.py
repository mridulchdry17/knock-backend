from __future__ import annotations

from typing import Literal

from pydantic import EmailStr

from app.schemas.common import ORMModel


class OnboardingStatusOut(ORMModel):
    """GET /api/v1/onboarding/status — frontend reads this for the auth-guard
    routing decision.

      - tier='super_admin'/'paid'/'free' → /dashboard
      - tier='pending' AND onboarded=False → /onboarding
      - tier='pending' AND onboarded=True → /awaiting-approval
    """

    tier: str
    waitlist_email: str | None
    onboarded: bool


class ClaimWaitlistIn(ORMModel):
    email: EmailStr


class AwaitingApprovalOut(ORMModel):
    status: Literal["awaiting_approval"] = "awaiting_approval"
