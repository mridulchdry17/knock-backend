"""Pydantic I/O contracts for /api/v1/preferences and /api/v1/autopilot/*.

`target_industries` is persisted as a JSON-encoded TEXT column. The Pydantic
boundary exposes it as `list[str]`; the service layer owns serialization.

`PreferencesPatch` uses `Optional[T] = None` semantics: a field omitted from
the request body is left untouched; an explicit `null` clears it. Pydantic's
`model_dump(exclude_unset=True)` is what makes that distinction work.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.time import utcnow
from app.schemas.common import ORMModel

# ─────────────────────────── stop-condition constants ───────────────────────────
# Kept co-located with the schema so validation errors and the frontend
# radio-group copy come from a single source. Mirrors the values in the
# spec (see project_phase5_send_model.md and the stop-conditions design doc).

STOP_TYPES = ("none", "replies", "end_date", "budget")
STOP_REPLIES_ALLOWED = frozenset({1, 3, 5})
STOP_BUDGET_ALLOWED = frozenset({25, 50, 100, 200})
# Users pick an end date at least one day in the future, at most 90 days out.
# Same-day would fire immediately on the very next cycle; > 90 days approaches
# the invisible platform ceiling and is pointless UX-wise.
STOP_DATE_MIN_OFFSET_DAYS = 1
STOP_DATE_MAX_OFFSET_DAYS = 90


class PreferencesOut(BaseModel):
    target_role: str | None = None
    target_industries: list[str] = Field(default_factory=list)
    target_location: str | None = None
    notify_gmail_disconnect: bool
    notify_daily_summary: bool
    autopilot_enabled: bool
    autopilot_paused_at: datetime | None = None
    autopilot_auto_pause_on_reply: bool
    autopilot_enabled_at: datetime | None = None
    autopilot_stop_type: Literal["none", "replies", "end_date", "budget"] = "none"
    autopilot_stop_at_replies: int | None = None
    autopilot_stop_at_date: date | None = None
    autopilot_stop_at_budget: int | None = None
    autopilot_paused_reason: str | None = None
    resume_url: str | None = None


class PreferencesPatch(BaseModel):
    target_role: str | None = None
    target_industries: list[str] | None = None
    target_location: str | None = None
    notify_gmail_disconnect: bool | None = None
    notify_daily_summary: bool | None = None
    autopilot_auto_pause_on_reply: bool | None = None
    resume_url: str | None = None

    # ─────────────── stop-condition fields ───────────────
    # Radio-group semantics: set stop_type to switch mode; provide the
    # matching value column. The service layer nulls the sibling value
    # columns so at most one is populated at any time (see
    # app.services.preferences.update_preferences).
    autopilot_stop_type: Literal["none", "replies", "end_date", "budget"] | None = None
    autopilot_stop_at_replies: int | None = None
    autopilot_stop_at_date: date | None = None
    autopilot_stop_at_budget: int | None = None

    @field_validator("autopilot_stop_at_replies")
    @classmethod
    def _check_replies(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if v not in STOP_REPLIES_ALLOWED:
            raise ValueError(
                f"stop_at_replies must be one of {sorted(STOP_REPLIES_ALLOWED)}"
            )
        return v

    @field_validator("autopilot_stop_at_budget")
    @classmethod
    def _check_budget(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if v not in STOP_BUDGET_ALLOWED:
            raise ValueError(
                f"stop_at_budget must be one of {sorted(STOP_BUDGET_ALLOWED)}"
            )
        return v

    @field_validator("autopilot_stop_at_date")
    @classmethod
    def _check_date(cls, v: date | None) -> date | None:
        if v is None:
            return v
        today = utcnow().date()
        min_date = today.fromordinal(today.toordinal() + STOP_DATE_MIN_OFFSET_DAYS)
        max_date = today.fromordinal(today.toordinal() + STOP_DATE_MAX_OFFSET_DAYS)
        if v < min_date or v > max_date:
            raise ValueError(
                f"stop_at_date must be between {min_date.isoformat()} "
                f"and {max_date.isoformat()}"
            )
        return v


class AddDomainIn(BaseModel):
    domain: str = Field(min_length=3, max_length=255)


class ExcludedDomainOut(ORMModel):
    domain: str
    created_at: datetime


class ExcludedDomainsListOut(BaseModel):
    items: list[ExcludedDomainOut]
