"""Pydantic I/O contracts for /api/v1/preferences and /api/v1/autopilot/*.

`target_industries` is persisted as a JSON-encoded TEXT column. The Pydantic
boundary exposes it as `list[str]`; the service layer owns serialization.

`PreferencesPatch` uses `Optional[T] = None` semantics: a field omitted from
the request body is left untouched; an explicit `null` clears it. Pydantic's
`model_dump(exclude_unset=True)` is what makes that distinction work.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class PreferencesOut(BaseModel):
    target_role: str | None = None
    target_industries: list[str] = Field(default_factory=list)
    target_location: str | None = None
    notify_gmail_disconnect: bool
    notify_daily_summary: bool
    autopilot_enabled: bool
    autopilot_paused_at: datetime | None = None
    autopilot_auto_pause_on_reply: bool
    resume_url: str | None = None


class PreferencesPatch(BaseModel):
    target_role: str | None = None
    target_industries: list[str] | None = None
    target_location: str | None = None
    notify_gmail_disconnect: bool | None = None
    notify_daily_summary: bool | None = None
    autopilot_auto_pause_on_reply: bool | None = None
    resume_url: str | None = None


class AddDomainIn(BaseModel):
    domain: str = Field(min_length=3, max_length=255)


class ExcludedDomainOut(ORMModel):
    domain: str
    created_at: datetime


class ExcludedDomainsListOut(BaseModel):
    items: list[ExcludedDomainOut]
