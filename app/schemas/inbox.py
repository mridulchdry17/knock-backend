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

from pydantic import BaseModel, ConfigDict, Field

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


class ThreadParticipantOut(BaseModel):
    """The recruiter side of the conversation, surfaced on ThreadDetailOut.sender.

    Matches the frontend's `ThreadParticipantSchema`:
      { name: nullable, email, role: nullable, company: nullable }

    `role` comes from contacts.role (job title we scraped/seeded); `company`
    is the company's display name (NOT the domain — the domain lives on the
    list row's snippet path, not here).
    """

    name: str | None
    email: str
    role: str | None
    company: str | None


ThreadMessageDirection = Literal["outbound", "inbound"]


class ThreadMessageOut(BaseModel):
    """One message in the detail view's mini-thread. Matches the frontend's
    `ThreadMessageSchema` 1:1 — same field naming convention used by /today
    and /templates: backend is pure I/O, no rendering or escaping. The
    `body_html` field carries the body as stored (Tiptap-authored HTML for
    outbound, Gmail text/plain for inbound) — exact field name from the Zod
    schema.

    Serialization quirk: `from_` → `from` via `serialization_alias` because
    `from` is a Python keyword. FastAPI dumps response models with
    `by_alias=True`, so the wire field is `from`.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str
    direction: ThreadMessageDirection
    from_: InboxSenderOut = Field(serialization_alias="from")
    body_html: str
    sent_at: datetime
    is_knock_drafted_followup: bool | None = None


class SuggestedFollowupOut(BaseModel):
    """Knock-drafted follow-up the user can one-click send. Reserved for a
    future iteration — the field is on ThreadDetailOut and always serialized
    as null in v0 (the frontend's Zod expects the key, just nullable)."""

    subject: str
    body_html: str
    reason: str


class ThreadDetailOut(BaseModel):
    """Conversation detail — matches the frontend's `ThreadDetailSchema`.

    `sender` is the recruiter we're talking to (the contact addressed by the
    original outbound). `suggested_followup` is always null in v0; the column
    is reserved so the wire shape stays stable when the feature lands.

    Note: extra backend-internal fields like `company_domain` / `can_reply`
    are NOT serialized — Zod silently strips them, but omitting keeps the wire
    contract tight.
    """

    id: str
    subject: str
    category: InboxCategoryLit
    sender: ThreadParticipantOut
    messages: list[ThreadMessageOut]
    suggested_followup: SuggestedFollowupOut | None = None


class ReplyIn(BaseModel):
    """Payload for POST /inbox/{id}/reply. `body_html` matches the frontend's
    Zod field name; contents are whatever the composer produces (Tiptap's
    HTML output by default). Backend is pass-through — `gmail_send.build_mime`
    flattens to text/plain for the actual Gmail send. Subject is optional —
    the router prefixes 'Re:' when omitted."""

    body_html: str
    subject: str | None = None


class ReplyResultOut(BaseModel):
    """Success-only shape — matches frontend's `ReplyResultSchema`:
      { ok: true, message_id: string }

    `message_id` is Gmail's API id for the newly-sent reply (was previously
    surfaced as `gmail_message_id`; renamed to match the frontend's field
    name). Failures come back as a structured ApiError (4xx/5xx), not this
    shape, so `ok` is always literally true here.
    """

    ok: Literal[True] = True
    message_id: str


class InboxSyncStatusOut(BaseModel):
    """Surface for the "last synced X mins ago" indicator.

    `last_synced_at` reflects the most recent reply we've recorded for this
    user (v0 proxy for "ingest completed"). `healthy` is required by the
    frontend; True whenever the endpoint can answer at all.
    """

    healthy: bool = True
    last_synced_at: datetime | None
