"""Pydantic I/O contracts for the /inbox router (B5.6).

Surfaces every send_queue row with status='REPLIED' so the user sees
exactly which outreaches got a reply, whether the reply was an explicit
stop, and the current lock state for the company domain.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class InboxItemOut(BaseModel):
    """One replied-to outreach in the user's inbox view."""

    id: int  # send_queue.id
    contact_name: str | None
    contact_email: str
    company_name: str
    company_domain: str
    subject: str
    replied_at: datetime
    reply_is_explicit_stop: bool
    # lock_status mirrors `services.locks.LockStatus`:
    #   "user_reply_lock" | "platform_permanent"
    # The cooldown / available states are not surfaced here — an inbox row
    # by definition got a reply, so one of those two locks must apply.
    lock_status: str
    locked_until: datetime | None


class InboxListOut(BaseModel):
    items: list[InboxItemOut]
    total: int
    limit: int
    offset: int
