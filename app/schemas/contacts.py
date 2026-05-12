"""Contacts schemas — user-facing (NOT admin).

B5.1b shipped per-user notes; B5.3 adds the browse/detail shapes used by the
read-only contact browser.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class MyContactNoteIn(BaseModel):
    # Empty string is permitted — the router treats it as a delete signal.
    # 5000 chars matches the column's effective ceiling for a single textarea
    # in the UI; longer values get a 422.
    notes: str = Field(min_length=0, max_length=5000)


class MyContactNoteOut(ORMModel):
    contact_id: int
    notes: str
    created_at: datetime
    updated_at: datetime


# ─────────────────────────── browse + detail (B5.3) ───────────────────────────


class ContactAvailability(BaseModel):
    """Lock state for a contact, surfaced inline on browse/detail rows.

    `available_at` is null for the two terminal states (AVAILABLE — already
    available; PLATFORM_PERMANENT — never available again). For the two
    time-bounded states it's an ISO datetime so UI can render countdowns.
    """

    status: Literal[
        "available", "platform_cooldown", "user_reply_lock", "platform_permanent"
    ]
    available_at: datetime | None
    reason: str | None


class ContactBrowseOut(ORMModel):
    """Light shape for the paginated list endpoint."""

    id: int
    email: str
    name: str | None
    role: str | None
    company_id: int
    company_name: str
    company_domain: str
    availability: ContactAvailability


class ContactDetailOut(ORMModel):
    """Full shape for /contacts/{id} — joins admin-curated notes + user's private notes."""

    id: int
    email: str
    name: str | None
    role: str | None
    company_id: int
    company_name: str
    company_domain: str
    linkedin_url: str | None
    shared_notes: str | None  # Contact.notes — admin-curated, shared
    my_notes: str | None  # UserContactNote.notes — private, may be null
    availability: ContactAvailability
