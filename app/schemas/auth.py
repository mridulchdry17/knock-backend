from __future__ import annotations

from app.schemas.common import ORMModel


class MeOut(ORMModel):
    """Returned by GET /api/v1/auth/me — minimal profile + onboarding flags."""

    id: int
    email: str
    full_name: str | None
    is_admin: bool
    onboarded: bool
    has_gmail_connected: bool
    daily_limit: int
    sent_today: int
