"""I/O contracts for /api/v1/templates — matches the F6 frontend verbatim.

snake_case wire shape. `id` is serialized as a string (frontend uses it as a
React key + path param). `used_count` / `reply_rate` are computed at read time,
not stored: used_count = today_batch_items referencing the template; reply_rate
is null in v0 (no per-template reply tracking yet).
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TemplateOut(BaseModel):
    id: str
    name: str
    subject: str
    body: str
    is_starter: bool
    used_count: int
    reply_rate: float | None
    created_at: datetime
    updated_at: datetime


class TemplatesListOut(BaseModel):
    items: list[TemplateOut]
    count: int
    cap: int


class TemplateInput(BaseModel):
    """POST body — all three required."""

    name: str = Field(min_length=1, max_length=255)
    subject: str = Field(min_length=1, max_length=512)
    # Generous but bounded — guards against a multi-MB body being sent via Gmail.
    body: str = Field(min_length=1, max_length=20_000)


class TemplatePatch(BaseModel):
    """PATCH body — any subset."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    subject: str | None = Field(default=None, min_length=1, max_length=512)
    body: str | None = Field(default=None, min_length=1, max_length=20_000)


class TestSendResult(BaseModel):
    sent: bool = True
