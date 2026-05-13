"""/api/v1/inbox — replies-only view of the user's send_queue.

A row appears here when the B5.6 reply ingestor flips its status to 'REPLIED'.
For each row we surface the current lock state for the company so the UI can
render "you can't email this company for 30 days" vs the permanent
"unsubscribed" banner without a second round-trip.

Tier-gated to free/paid/super_admin via the router-level dependency.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import desc, func, select

from app.core.deps import CurrentUser, DbDep, require_tier
from app.core.pagination import PaginationParams, pagination
from app.logging_config import get_logger
from app.models import Company, Contact, SendQueue
from app.schemas.admin import Page
from app.schemas.inbox import InboxItemOut, InboxSyncStatusOut
from app.services import locks as locks_svc

router = APIRouter(
    prefix="/api/v1/inbox",
    tags=["inbox"],
    dependencies=[Depends(require_tier("free", "paid", "super_admin"))],
)

log = get_logger("inbox")


@router.get("", response_model=Page[InboxItemOut])
def list_replies(
    user: CurrentUser,
    db: DbDep,
    page: Annotated[PaginationParams, Depends(pagination)],
) -> Page[InboxItemOut]:
    """Newest-first replies for the current user.

    Returns 200 with empty items list when the user has no replies yet —
    do NOT 404, the empty state is a valid view.
    """
    rows = list(
        db.scalars(
            select(SendQueue)
            .where(SendQueue.user_id == user.id)
            .where(SendQueue.status == "REPLIED")
            .order_by(desc(SendQueue.replied_at))
            .limit(page.limit)
            .offset(page.offset)
        ).all()
    )
    total = (
        db.scalar(
            select(func.count())
            .select_from(SendQueue)
            .where(SendQueue.user_id == user.id)
            .where(SendQueue.status == "REPLIED")
        )
        or 0
    )

    # Bulk-hydrate contacts + companies in two SELECT IN queries; UI can show
    # up to `limit` rows so the N here is bounded by the page size.
    contact_ids = {r.to_contact_id for r in rows if r.to_contact_id is not None}
    if not contact_ids:
        contact_ids = {r.contact_id for r in rows if r.contact_id is not None}
    contacts_by_id: dict[int, Contact] = {}
    if contact_ids:
        contacts_by_id = {
            c.id: c
            for c in db.scalars(select(Contact).where(Contact.id.in_(contact_ids))).all()
        }

    company_ids = {c.company_id for c in contacts_by_id.values()}
    companies_by_id: dict[int, Company] = {}
    if company_ids:
        companies_by_id = {
            c.id: c
            for c in db.scalars(select(Company).where(Company.id.in_(company_ids))).all()
        }

    items: list[InboxItemOut] = []
    for r in rows:
        contact = contacts_by_id.get(r.to_contact_id or r.contact_id or 0)
        company = (
            companies_by_id.get(contact.company_id)
            if contact is not None
            else None
        )

        # Lock state — derive at read time so admin clears flow through.
        avail = locks_svc.check_can_send_to_company(
            db, user_id=user.id, company_domain=(r.company_domain or "")
        )

        items.append(
            InboxItemOut(
                id=r.id,
                contact_name=(contact.name if contact else None),
                contact_email=(contact.email if contact else "") or "",
                company_name=(company.name if company else ""),
                company_domain=r.company_domain or "",
                subject=r.subject or "",
                replied_at=r.replied_at,  # always present when status='REPLIED'
                reply_is_explicit_stop=bool(r.reply_is_explicit_stop),
                lock_status=avail.status.value,
                locked_until=avail.unlocked_at,
            )
        )

    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


@router.get("/sync-status", response_model=InboxSyncStatusOut)
def get_sync_status(user: CurrentUser, db: DbDep) -> InboxSyncStatusOut:
    """Powers the "synced X mins ago" indicator on the inbox.

    v0: `last_synced_at` is derived from the most recent `replied_at` we've
    recorded for this user — there's no separate "last ingest run completed"
    column yet. That makes the indicator accurate when replies are flowing and
    null on a quiet day; either is acceptable for the launch UX.

    `syncing` is always False in v0 — single-process worker, no concurrent
    ingest. Reserved for v1 when async background ingest lands.
    """
    last_replied = db.scalar(
        select(SendQueue.replied_at)
        .where(SendQueue.user_id == user.id)
        .where(SendQueue.status == "REPLIED")
        .order_by(desc(SendQueue.replied_at))
        .limit(1)
    )
    return InboxSyncStatusOut(last_synced_at=last_replied, syncing=False)
