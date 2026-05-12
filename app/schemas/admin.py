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


# ─────────────────────────── contacts (B5.1) ───────────────────────────


class ContactUploadIn(BaseModel):
    # Untyped row dicts on purpose: the upload service owns column aliasing +
    # validation and the admin UI sends heterogeneous shapes.
    rows: list[dict[str, str | None]]
    dry_run: bool = False


class RowErrorOut(BaseModel):
    row_index: int
    email: str | None
    error_code: str
    message: str


class ContactUploadResultOut(BaseModel):
    inserted: int
    updated: int
    skipped: int
    failed: int
    row_errors: list[RowErrorOut]


class AdminContactOut(BaseModel):
    """Admin-only view of a contact. Includes company name/domain via join."""

    id: int
    email: str
    name: str | None
    role: str | None  # admin UI labels this "Title" (matches user vocabulary)
    company_id: int
    company_name: str
    company_domain: str
    linkedin_url: str | None
    source: str | None
    notes: str | None
    scraped_pattern: str | None
    is_invalid: bool
    created_at: datetime


class CompanySummaryOut(BaseModel):
    company_id: int
    company_name: str
    company_domain: str
    contact_count: int
