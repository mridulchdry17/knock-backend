"""/api/v1/inbox — replies + bounces for the current user.

Sources rows from `send_queue` and projects them into the F.7 frontend wire
shape (see `app/schemas/inbox.py`). Categories:
  - reply  ← status='REPLIED'
  - bounce ← status='BOUNCED' (set by the reply ingestor's _handle_bounce)
  - nudge  ← reserved; no data yet, the filter returns empty 200.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select

from app.core.deps import CurrentUser, DbDep, require_tier
from app.core.pagination import PaginationParams, pagination
from app.core.time import ensure_utc
from app.logging_config import get_logger
from app.models import Contact, SendQueue
from app.schemas.inbox import (
    InboxCategoryLit,
    InboxItemOut,
    InboxListOut,
    InboxSenderOut,
    InboxSyncStatusOut,
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
