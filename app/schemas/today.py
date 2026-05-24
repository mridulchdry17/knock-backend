"""Pydantic I/O contracts for the /today router.

Matches the F.5a frontend contract (project_phase5_send_model.md): each
TodayItemOut represents ONE company outreach with a TO recipient and 0..4 CC
recipients — not one card per contact.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

StatusLiteral = Literal[
    "default", "ready", "skipped", "sent", "cooldown", "held"
]


class RecipientOut(BaseModel):
    """One recipient (TO or CC) on a company outreach card."""

    name: str | None
    email: str
    role: str | None
    company: str
    company_domain: str
    linkedin_url: str | None
    # Reserved for the future scraper; always null in v0.
    avatar_url: str | None = None


class TodayItemOut(BaseModel):
    # Serialize as string so the frontend can use it as a stable React key
    # without worrying about int overflow / coercion in the URL bar.
    id: str
    recipient: RecipientOut  # TO recipient
    cc_recipients: list[RecipientOut]
    template_id: str | None
    template_name: str | None
    subject: str
    body_preview: str
    body: str
    send_time: datetime
    status: StatusLiteral
    cooldown_until: datetime | None
    sent_at: datetime | None


class TodayBatchOut(BaseModel):
    generated_at: datetime
    date: date
    cap: int
    sent_today: int
    items: list[TodayItemOut]


class UpdateItemIn(BaseModel):
    """Partial update for a card. Marking any field auto-promotes status to
    'ready' (router-level concern, not enforced here)."""

    subject: str | None = None
    body: str | None = None
    status: Literal["default", "ready", "skipped"] | None = None
    send_time: datetime | None = None
    template_id: int | None = None


class SendBatchResultOut(BaseModel):
    """Response for POST /today/send-batch. Matches the F.5b frontend contract
    (BatchDispatchResultSchema)."""

    dispatched_count: int
    scheduled_first_at: datetime
    scheduled_last_at: datetime


class SkipTodayResultOut(BaseModel):
    """Response for POST /today/skip. Matches the frontend SkipTodayResultSchema
    ({ skipped: true })."""

    skipped: Literal[True] = True


class AdminCronRunResultItemOut(BaseModel):
    user_id: int
    items_created: int
    items_skipped: int
    reason_if_skipped: str | None


class AdminCronRunResultOut(BaseModel):
    batch_date: date
    results: list[AdminCronRunResultItemOut]
    total_items_created: int
    total_users_processed: int
