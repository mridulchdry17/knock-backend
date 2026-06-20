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


class RefreshOut(ORMModel):
    """Returned by POST /api/v1/auth/refresh — the freshly-minted access token.
    The new refresh token is set as an HttpOnly cookie on the same response;
    only the access token is JSON-readable (frontend stores it in memory)."""

    access_token: str
