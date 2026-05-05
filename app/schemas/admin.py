"""Admin (super_admin only) schemas — kept separate from public schemas
because admin endpoints expose more user state (tier, waitlist_email,
session counts, etc.) than the regular `/auth/me` response."""
from __future__ import annotations

from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel

from app.schemas.common import ORMModel

Tier = Literal["pending", "free", "paid", "super_admin"]

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Generic paginated envelope. Phase 5 listings will reuse this."""

    items: list[T]
    total: int
    limit: int
    offset: int


class AdminUserOut(ORMModel):
    id: int
    email: str
    full_name: str | None
    tier: Tier
    waitlist_email: str | None
    is_suspended: bool
    has_gmail_connected: bool
    created_at: datetime
    tier_set_at: datetime | None


class TierUpdateIn(BaseModel):
    tier: Tier


class AdminWaitlistOut(ORMModel):
    id: int
    email: str
    created_at: datetime
