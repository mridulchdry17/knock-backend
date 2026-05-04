from __future__ import annotations

from app.schemas.common import ORMModel


class MeOut(ORMModel):
    """Returned by GET /api/v1/auth/me — minimal profile + tier + onboarding flags."""

    id: int
    email: str
    full_name: str | None
    tier: str  # 'pending' | 'free' | 'paid' | 'super_admin'
    onboarded: bool  # waitlist_email IS NOT NULL
    has_gmail_connected: bool
    daily_limit: int
    sent_today: int
