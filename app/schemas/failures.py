"""Schemas for the B5.5 admin failures dashboard + drain endpoint."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class FailureOut(BaseModel):
    """One row of the email_failures table, joined with user.email."""

    id: int
    user_id: int
    user_email: str
    today_batch_item_id: int | None
    company_domain: str
    failure_kind: str
    error_message: str
    gmail_error_code: str | None
    created_at: datetime


class FailureSummaryOut(BaseModel):
    """Counts by failure_kind across two rolling windows.

    Each value is a dict[failure_kind, count]. Kinds with zero count over a
    window are absent (rather than zero-padded) — frontend can default to 0.
    """

    last_24h: dict[str, int]
    last_7d: dict[str, int]


class DrainSummaryOut(BaseModel):
    attempted: int
    sent: int
    failed: int
    skipped: int
    failures_by_kind: dict[str, int]
