"""Pydantic I/O contracts for the /inbox router.

Matches the F.7 frontend wire shape (`lib/inbox/types.ts`) so the page can
parse the list response without snagging. The inbox surfaces three message
categories:

  - `reply`  — recruiter wrote back (send_queue.status='REPLIED')
  - `bounce` — Gmail bounce / postmaster notice (send_queue.status='BOUNCED')
  - `nudge`  — Knock-flagged "follow-up suggested" item (not implemented yet
               — the filter is accepted but currently always returns empty)
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

InboxCategoryLit = Literal["reply", "bounce", "nudge"]


class InboxSenderOut(BaseModel):
    """Minimal sender identity for the list row. Matches the frontend
    `InboxSenderSchema` exactly — just name + email."""

    name: str | None
    email: str


class InboxItemOut(BaseModel):
    """One row in the inbox list. `id` is serialized as a string (React key /
    path param); `message_count` is 1 for v0 (we don't track thread length
    yet); `unread` defaults False until we add an unread column."""

    id: str
    category: InboxCategoryLit
    subject: str
    sender: InboxSenderOut
    snippet: str
    last_message_at: datetime
    unread: bool = False
    message_count: int = 1


class InboxListOut(BaseModel):
    """Envelope for the list endpoint. `unread_count` is required by the
    frontend Zod schema (the missing-field was the snag); always 0 in v0."""

    items: list[InboxItemOut]
    total: int
    unread_count: int = 0


class InboxSyncStatusOut(BaseModel):
    """Surface for the "last synced X mins ago" indicator.

    `last_synced_at` reflects the most recent reply we've recorded for this
    user (v0 proxy for "ingest completed"). `healthy` is required by the
    frontend; True whenever the endpoint can answer at all.
    """

    healthy: bool = True
    last_synced_at: datetime | None
