"""/api/v1/inbox — replies + bounces for the current user.

Sources rows from `send_queue` and projects them into the F.7 frontend wire
shape (see `app/schemas/inbox.py`). Categories:
  - reply  ← status='REPLIED'
  - bounce ← status='BOUNCED' (set by the reply ingestor's _handle_bounce)
  - nudge  ← reserved; no data yet, the filter returns empty 200.
"""
from __future__ import annotations

import html as _html
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import desc, func, select

from app.core.deps import CurrentUser, DbDep, require_tier
from app.core.errors import ApiError
from app.core.pagination import PaginationParams, pagination
from app.core.time import ensure_utc
from app.logging_config import get_logger
from app.models import Contact, SendQueue, User
from app.schemas.inbox import (
    InboxCategoryLit,
    InboxItemOut,
    InboxListOut,
    InboxSenderOut,
    InboxSyncStatusOut,
    ThreadDetailOut,
    ThreadMessageOut,
    ThreadParticipantOut,
)

router = APIRouter(
    prefix="/api/v1/inbox",
    tags=["inbox"],
    dependencies=[Depends(require_tier("free", "paid", "super_admin"))],
)

log = get_logger("inbox")

# Status → frontend category mapping. The inbox only surfaces rows in these
# statuses; sent-but-no-response and skipped rows live in /today, not here.
_STATUS_TO_CATEGORY: dict[str, InboxCategoryLit] = {
    "REPLIED": "reply",
    "BOUNCED": "bounce",
}


def _to_html(text: str | None) -> str:
    """Wrap plain text into minimal HTML so the frontend's ThreadMessage view
    can render outbound + inbound uniformly. Each blank-line-separated block
    becomes a <p>, with single newlines preserved as <br/>. Escaped so any
    angle brackets in the body don't break rendering."""
    if not text:
        return ""
    paragraphs = [p for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    return "".join(
        "<p>" + _html.escape(p).replace("\n", "<br/>") + "</p>" for p in paragraphs
    )


def _snippet(body_text: str | None, *, length: int = 140) -> str:
    """One-line preview for the list row. Trim to a single line + length cap."""
    if not body_text:
        return ""
    flat = " ".join(body_text.split())  # collapse whitespace/newlines
    if len(flat) <= length:
        return flat
    return flat[: length - 1].rstrip() + "…"


@router.get("", response_model=InboxListOut)
def list_inbox(
    user: CurrentUser,
    db: DbDep,
    page: Annotated[PaginationParams, Depends(pagination)],
    category: Annotated[InboxCategoryLit | None, Query()] = None,
) -> InboxListOut:
    """Newest-first list of inbox items for the current user.

    `category=reply|bounce|nudge` filters the rows; omit it for All (replies
    + bounces). Empty result is a valid 200 — frontend renders 'All caught up.'
    """
    # Map category → status filter.
    if category == "reply":
        statuses: list[str] = ["REPLIED"]
    elif category == "bounce":
        statuses = ["BOUNCED"]
    elif category == "nudge":
        # No nudge data in v0 — return empty 200 so the tab doesn't snag.
        return InboxListOut(items=[], total=0, unread_count=0)
    else:  # None / 'all'
        statuses = ["REPLIED", "BOUNCED"]

    # COALESCE replied_at, sent_at — bounces don't set replied_at, so order by
    # whichever timestamp the row has. Newest first.
    order_ts = func.coalesce(SendQueue.replied_at, SendQueue.sent_at)

    rows = list(
        db.scalars(
            select(SendQueue)
            .where(SendQueue.user_id == user.id)
            .where(SendQueue.status.in_(statuses))
            .order_by(desc(order_ts))
            .limit(page.limit)
            .offset(page.offset)
        ).all()
    )
    total = (
        db.scalar(
            select(func.count())
            .select_from(SendQueue)
            .where(SendQueue.user_id == user.id)
            .where(SendQueue.status.in_(statuses))
        )
        or 0
    )

    # Bulk-hydrate the TO contact for each row in one query (no N+1).
    contact_ids = {r.to_contact_id for r in rows if r.to_contact_id is not None}
    contacts_by_id: dict[int, Contact] = {}
    if contact_ids:
        contacts_by_id = {
            c.id: c
            for c in db.scalars(select(Contact).where(Contact.id.in_(contact_ids))).all()
        }

    items: list[InboxItemOut] = []
    for r in rows:
        contact = contacts_by_id.get(r.to_contact_id or 0)
        sender = InboxSenderOut(
            name=(contact.name if contact else None),
            email=(contact.email if contact else "") or "",
        )
        cat: InboxCategoryLit = _STATUS_TO_CATEGORY.get(r.status, "reply")
        # libsql strips tzinfo on storage — re-attach UTC at the boundary.
        # Fallback chain: replied_at (REPLIED rows have it) → sent_at (BOUNCED
        # rows always do — they came from already-dispatched send_queue rows)
        # → created_at (NOT NULL via the mixin; defensive last-resort so the
        # response schema can never see a missing timestamp).
        ts = ensure_utc(r.replied_at or r.sent_at or r.created_at)
        items.append(
            InboxItemOut(
                id=str(r.id),
                category=cat,
                subject=r.subject or "",
                sender=sender,
                snippet=_snippet(r.body_text),
                last_message_at=ts,
                unread=False,  # no unread tracking in v0
                message_count=1,
            )
        )

    return InboxListOut(items=items, total=total, unread_count=0)


@router.get("/sync-status", response_model=InboxSyncStatusOut)
def get_sync_status(user: CurrentUser, db: DbDep) -> InboxSyncStatusOut:
    """Powers the "synced X mins ago" indicator on the inbox.

    v0: `last_synced_at` is the most recent `replied_at` we've recorded for
    this user — accurate when replies are flowing, null on a quiet day.
    `healthy` is True whenever the endpoint can answer.

    NOTE — declared BEFORE the /{item_id} route on purpose: item_id is typed as
    `int`, so FastAPI would 422 the literal path /sync-status if /{item_id}
    were registered first (it tries to coerce 'sync-status' to int rather than
    falling through to the next route).
    """
    last_replied = db.scalar(
        select(SendQueue.replied_at)
        .where(SendQueue.user_id == user.id)
        .where(SendQueue.status == "REPLIED")
        .order_by(desc(SendQueue.replied_at))
        .limit(1)
    )
    return InboxSyncStatusOut(
        healthy=True,
        last_synced_at=ensure_utc(last_replied) if last_replied else None,
    )


def _load_owned_row(db, user: User, item_id: int) -> SendQueue:
    """Fetch a send_queue row scoped to the current user, or raise 404.

    Single 404 for both 'not found' and 'belongs to another user' — leaking
    existence is the kind of side channel that doesn't matter much for an
    outreach tool but costs nothing to close.
    """
    sq = db.scalar(
        select(SendQueue).where(SendQueue.id == item_id, SendQueue.user_id == user.id)
    )
    if sq is None:
        raise ApiError(
            "not_found", "Inbox item not found.", status_code=status.HTTP_404_NOT_FOUND
        )
    return sq


@router.get("/{item_id}", response_model=ThreadDetailOut)
def get_thread(item_id: int, user: CurrentUser, db: DbDep) -> ThreadDetailOut:
    """Detail view for one inbox row — our outbound plus the latest inbound reply.

    Scope is intentionally narrow: we surface the conversation Knock initiated
    (the send_queue row + its denormalized reply), not the full Gmail thread.
    Bounces show only the outbound (no real recipient replied); reply rows show
    both messages when the reply body was ingested.
    """
    sq = _load_owned_row(db, user, item_id)

    # Only REPLIED + BOUNCED rows belong to the inbox surface — SENT-with-no-
    # response lives on /today, and clicking through here would be misleading.
    if sq.status not in ("REPLIED", "BOUNCED"):
        raise ApiError(
            "not_found", "Inbox item not found.", status_code=status.HTTP_404_NOT_FOUND
        )

    category: InboxCategoryLit = _STATUS_TO_CATEGORY.get(sq.status, "reply")

    # Sender for the outbound is the current user (the Knock user — that's us).
    # `user.name` may be missing for OAuth users we never asked to name; fall
    # back to None and let the frontend render just the email.
    outbound_sender = ThreadParticipantOut(
        name=getattr(user, "name", None) or None,
        email=user.email,
    )

    outbound = ThreadMessageOut(
        direction="outbound",
        sender=outbound_sender,
        subject=sq.subject or "",
        body_text=sq.body_text or "",
        body_html=_to_html(sq.body_text),
        sent_at=ensure_utc(sq.sent_at or sq.created_at),
    )

    messages: list[ThreadMessageOut] = [outbound]

    # Inbound — present only when the reply ingestor stored a body for this row.
    # Bounce rows never get a reply_body_text (the ingestor's bounce path takes
    # a different branch), so this naturally yields a single-message thread for
    # them. The to_contact lookup gives us a name for the inbound when the
    # reply came from the same contact we addressed.
    if sq.reply_body_text:
        inbound_email = sq.reply_from_email or ""
        inbound_name: str | None = None
        if sq.to_contact_id is not None:
            contact = db.get(Contact, sq.to_contact_id)
            if contact is not None:
                # Only attach the contact's name when the reply actually came
                # from that contact's address — otherwise the reply is from
                # someone else on the thread and we don't know their name.
                if contact.email and inbound_email and (
                    contact.email.lower() == inbound_email.lower()
                ):
                    inbound_name = contact.name
                # Fallback: no from_email persisted (legacy rows) — assume the
                # contact we addressed is who replied.
                if not inbound_email and contact.email:
                    inbound_email = contact.email
                    inbound_name = contact.name

        inbound_ts = ensure_utc(
            sq.reply_internal_date or sq.replied_at or sq.sent_at or sq.created_at
        )
        messages.append(
            ThreadMessageOut(
                direction="inbound",
                sender=ThreadParticipantOut(name=inbound_name, email=inbound_email),
                subject=sq.subject or "",  # threading subject; same as outbound's
                body_text=sq.reply_body_text,
                body_html=_to_html(sq.reply_body_text),
                sent_at=inbound_ts,
            )
        )

    # User's outbound reply from Knock (POST /inbox/{id}/reply). Surfaced AFTER
    # the inbound because chronologically a reply comes after the message it's
    # responding to; v0 stores only the most recent outbound reply so this is
    # at most one row.
    if sq.outbound_reply_text:
        messages.append(
            ThreadMessageOut(
                direction="outbound",
                sender=outbound_sender,
                subject=sq.subject or "",
                body_text=sq.outbound_reply_text,
                body_html=_to_html(sq.outbound_reply_text),
                sent_at=ensure_utc(
                    sq.outbound_reply_sent_at or sq.replied_at or sq.created_at
                ),
            )
        )

    # Replying needs a Gmail thread to land on + (ideally) an RFC822 Message-ID
    # for the In-Reply-To header. Bounces can't be replied to (no human at the
    # other end). Legacy rows without a thread id can't thread reliably either.
    can_reply = (
        sq.status == "REPLIED"
        and sq.gmail_thread_id is not None
        and bool(sq.reply_body_text)
    )

    return ThreadDetailOut(
        id=str(sq.id),
        category=category,
        subject=sq.subject or "",
        company_domain=sq.company_domain,
        can_reply=can_reply,
        messages=messages,
    )
