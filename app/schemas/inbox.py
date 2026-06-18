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


class ThreadParticipantOut(BaseModel):
    """One side of the conversation — used for both the outbound (us) and the
    inbound (recruiter). `name` is best-effort: we have it for contacts in the
    pool but the reply ingestor only stores the inbound from_email, so the
    reply's name may be None on the wire."""

    name: str | None
    email: str


ThreadMessageDirection = Literal["outbound", "inbound"]


class ThreadMessageOut(BaseModel):
    """One message in the detail view's mini-thread.

    `body_html` is the rendered body. Our outbound is stored as plain text; we
    wrap it in minimal `<p>` markup at response time so the frontend can render
    it consistently. `body_text` is also returned for any consumer that needs
    the raw form (e.g. populating the reply composer with a quoted block).
    """

    direction: ThreadMessageDirection
    sender: ThreadParticipantOut
    subject: str
    body_text: str
    body_html: str
    sent_at: datetime


class ThreadDetailOut(BaseModel):
    """Conversation detail. v0 surfaces at most two messages — the outbound we
    sent and the most-recent inbound reply (if any). `category` is the same
    enum the list endpoint uses so the page can tag the header consistently.
    `can_reply` is False for bounces (no live recipient to reply to) and for
    any row missing a Gmail thread id."""

    id: str
    category: InboxCategoryLit
    subject: str
    company_domain: str | None
    can_reply: bool
    messages: list[ThreadMessageOut]


class ReplyIn(BaseModel):
    """Payload for POST /inbox/{id}/reply. `body_text` is plain text authored
    by the user in the Knock composer; we own the threading headers and the
    Gmail thread linkage server-side — the client only sends the message body
    (and an optional subject override for the rare case the user wants to
    rename the Re: line)."""

    body_text: str
    subject: str | None = None


class ReplyResultOut(BaseModel):
    """Outcome of a reply send. Mirrors send_email's success path — the new
    Gmail message id + the same thread id the original used. Failures come
    back as a structured ApiError (4xx/5xx), not this shape."""

    ok: bool
    gmail_message_id: str | None
    gmail_thread_id: str | None


class InboxSyncStatusOut(BaseModel):
    """Surface for the "last synced X mins ago" indicator.

    `last_synced_at` reflects the most recent reply we've recorded for this
    user (v0 proxy for "ingest completed"). `healthy` is required by the
    frontend; True whenever the endpoint can answer at all.
    """

    healthy: bool = True
    last_synced_at: datetime | None
