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
    # NULL = on the list, not allowed in yet. Set when a super_admin clicks Allow.
    approved_at: datetime | None = None
    # What tier this entry will grant on sign-in. 'free' is the default; admin
    # can pre-mark 'paid' so the user lands paid without a second click.
    intended_tier: Literal["free", "paid"] = "free"


class ApproveWaitlistIn(BaseModel):
    """Optional body for POST /admin/waitlist/{id}/approve. Empty body keeps
    the legacy default (free)."""

    tier: Literal["free", "paid"] = "free"


class BulkApproveWaitlistIn(BaseModel):
    """POST /admin/waitlist/approve-bulk — approve N rows in one call. All
    rows get the same `tier` (defaults to 'free'); per-row tier overrides
    would need a different endpoint shape."""

    ids: list[int]
    tier: Literal["free", "paid"] = "free"


class BulkApproveWaitlistOut(BaseModel):
    """Result of a bulk approval. Idempotent: previously-approved rows count
    toward `already_approved`, not `newly_approved`."""

    newly_approved: int
    already_approved: int
    not_found_ids: list[int]


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
    invalid_reason: str | None = None
    created_at: datetime


class CompanySummaryOut(BaseModel):
    company_id: int
    company_name: str
    company_domain: str
    contact_count: int


# ─────────────────────────── locks (B5.3) ───────────────────────────


class AdminGlobalLockOut(BaseModel):
    """Active 36h platform cooldown row (admin view)."""

    company_domain: str
    locked_at: datetime
    locked_until: datetime
    last_locked_by_user_id: int | None


class AdminPlatformLockOut(BaseModel):
    """Platform-wide permanent stop (admin view)."""

    company_domain: str
    reason: str
    created_at: datetime


class AdminUserCompanyLockOut(BaseModel):
    user_id: int
    company_domain: str
    locked_at: datetime
    locked_until: datetime
    is_permanent: bool
    reason: str


# ─────────────────────────── reply ingest (B5.6) ───────────────────────────


class IngestSummaryOut(BaseModel):
    """One user's reply ingest summary (admin endpoint response item)."""

    user_id: int
    processed: int
    replies_matched: int
    explicit_stops: int
    user_reply_locks_written: int
    error_kind: str | None = None
