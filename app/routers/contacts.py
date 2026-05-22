"""Contacts router — user-facing endpoints.

B5.1b shipped the per-user notes endpoints (`/{contact_id}/my-notes`).
B5.3 adds the read-only contact browser (list + detail) on this same router.

Layered: router validates payload + ownership, service owns lock semantics,
repositories own SQL. The browse endpoint pre-filters exclusions/locks at
the SQL layer (one round-trip) and surfaces 36h cooldown state per-row via
`availability` — see `list_available_contacts` for the rationale.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.deps import CurrentUser, DbDep, require_tier
from app.core.errors import ApiError
from app.core.pagination import PaginationParams, pagination
from app.core.time import utcnow
from app.logging_config import get_logger
from app.repositories import contacts as contacts_repo
from app.repositories import locks as locks_repo
from app.repositories import preferences as prefs_repo
from app.repositories import user_contact_notes as notes_repo
from app.schemas.admin import Page
from app.schemas.common import Ok
from app.schemas.contacts import (
    ContactAvailability,
    ContactBrowseOut,
    ContactDetailOut,
    MyContactNoteIn,
    MyContactNoteOut,
)
from app.services import locks as locks_svc

router = APIRouter(
    prefix="/api/v1/contacts",
    tags=["contacts"],
    # Pending users see 403 on every feature route until approved. Free/paid/
    # super_admin all get through. Frontend route guard (F.2) handles the
    # 403 -> /pending redirect.
    dependencies=[Depends(require_tier("free", "paid", "super_admin"))],
)

log = get_logger("contacts")


def _require_contact_exists(db, contact_id: int) -> None:
    if contacts_repo.get(db, contact_id) is None:
        raise ApiError(
            "not_found",
            "Contact not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )


def _availability_from_lock(result: locks_svc.LockCheckResult) -> ContactAvailability:
    return ContactAvailability(
        status=result.status.value,  # type: ignore[arg-type]
        available_at=result.unlocked_at,
        reason=result.reason,
    )


# ─────────────────────────── browse (B5.3) ───────────────────────────


@router.get("", response_model=Page[ContactBrowseOut])
def list_available_contacts(
    user: CurrentUser,
    db: DbDep,
    page: Annotated[PaginationParams, Depends(pagination)],
    search: Annotated[str | None, Query(max_length=128)] = None,
    company_domain: Annotated[str | None, Query(max_length=255)] = None,
) -> Page[ContactBrowseOut]:
    """User-facing browse — read-only view of the admin-curated contact pool.

    Hard-filtered out (will not appear at all):
      - Contacts whose company domain is in the user's excluded-domains list
      - Contacts under a platform-permanent lock (explicit-stop)
      - Contacts under this user's per-user reply lock (active rows only —
        permanent flag or `locked_until > now`)
      - Invalid contacts (`is_invalid=True`)

    Soft-surfaced via `availability` (still listed, but with status):
      - 36h platform cooldown — user can see "available in 12h"

    Rationale for the soft-surface vs hard-filter split: the platform cooldown
    is short and rolling; hiding contacts during it would create a churny UX.
    Permanent and per-user reply locks are long-lived and contextually
    actionable elsewhere (Inbox), so hiding them is correct.

    Tier-gated to free/paid/super_admin via the router-level dependency.
    """
    now = utcnow()

    # Build the pre-filter domain set in one pass — caller-side merge avoids
    # three SQL round-trips for what's typically a small set.
    excluded_rows = prefs_repo.list_excluded_domains(db, user.id)
    excluded_domains: set[str] = {row.domain for row in excluded_rows}

    platform_locks = locks_repo.list_platform_locks(db)
    excluded_domains.update(lock.company_domain for lock in platform_locks)

    user_locks = locks_repo.list_active_user_locks(db, user.id, now=now)
    excluded_domains.update(lock.company_domain for lock in user_locks)

    pairs, total = contacts_repo.list_browse_paginated(
        db,
        exclude_domains=excluded_domains,
        search=search,
        company_domain=company_domain,
        limit=page.limit,
        offset=page.offset,
    )

    # Per-row availability: the only remaining possibility on browse rows is
    # AVAILABLE or PLATFORM_COOLDOWN (the other two states were hard-filtered).
    # Batched into 3 queries for the whole page (not 3-per-row); the result
    # shape stays consistent with detail, and any future enum status lights up
    # here without a router change.
    page_domains = {company.domain for _, company in pairs}
    lock_by_domain = locks_svc.check_can_send_to_companies(
        db, user_id=user.id, company_domains=page_domains
    )

    items: list[ContactBrowseOut] = []
    for contact, company in pairs:
        avail = lock_by_domain.get(company.domain.strip().lower()) or locks_svc.LockCheckResult(
            status=locks_svc.LockStatus.AVAILABLE, unlocked_at=None, reason=None
        )
        items.append(
            ContactBrowseOut(
                id=contact.id,
                email=contact.email or "",
                name=contact.name,
                role=contact.role,
                company_id=company.id,
                company_name=company.name,
                company_domain=company.domain,
                availability=_availability_from_lock(avail),
            )
        )

    return Page(items=items, total=total, limit=page.limit, offset=page.offset)


@router.get("/{contact_id}", response_model=ContactDetailOut)
def get_contact_detail(
    contact_id: int, user: CurrentUser, db: DbDep
) -> ContactDetailOut:
    """Hydrates: contact row + company + admin-curated notes + user's private notes + lock state.

    Detail differs from browse in two ways:
      - It does NOT hard-filter — admins/users following a deep link to a
        locked contact should see WHY it's locked, not 404.
      - It surfaces the full lock state (including PLATFORM_PERMANENT and
        USER_REPLY_LOCK) so the UI can render the appropriate locked-state
        microcopy.
    """
    pair = contacts_repo.get_with_company(db, contact_id)
    if pair is None:
        raise ApiError(
            "not_found",
            "Contact not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    contact, company = pair

    my_note = notes_repo.get(db, user.id, contact_id)
    avail = locks_svc.check_can_send_to_company(
        db, user_id=user.id, company_domain=company.domain
    )

    return ContactDetailOut(
        id=contact.id,
        email=contact.email or "",
        name=contact.name,
        role=contact.role,
        company_id=company.id,
        company_name=company.name,
        company_domain=company.domain,
        linkedin_url=contact.linkedin_url,
        shared_notes=contact.notes,
        my_notes=my_note.notes if my_note is not None else None,
        availability=_availability_from_lock(avail),
    )


# ─────────────────────────── per-user notes ───────────────────────────


@router.get("/{contact_id}/my-notes", response_model=MyContactNoteOut)
def get_my_note(contact_id: int, user: CurrentUser, db: DbDep) -> MyContactNoteOut:
    """Returns this user's private note for the contact, or 404 if none exists.

    Validates the contact exists first — without that check we'd 404 with the
    same message for both 'contact deleted' and 'never wrote a note', which
    confuses the frontend.
    """
    _require_contact_exists(db, contact_id)
    row = notes_repo.get(db, user.id, contact_id)
    if row is None:
        raise ApiError(
            "not_found",
            "You haven't added a note for this contact yet.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return MyContactNoteOut.model_validate(row)


@router.put(
    "/{contact_id}/my-notes",
    response_model=MyContactNoteOut,
    responses={204: {"description": "Note cleared (empty payload deletes)."}},
)
def upsert_my_note(
    contact_id: int, payload: MyContactNoteIn, user: CurrentUser, db: DbDep
) -> MyContactNoteOut | Response:
    """Idempotent upsert. Empty string deletes the note (returns 204).

    Why empty-string-as-delete: the F.10 textarea UX is "clear field + save"
    rather than a separate trash button. Backing that with PUT-empty here
    keeps the frontend single-codepath. Explicit DELETE is still available
    for callers that want it.
    """
    _require_contact_exists(db, contact_id)

    text = payload.notes.strip()
    if not text:
        notes_repo.delete_row(db, user.id, contact_id)
        db.commit()
        log.info("contacts.my_note_cleared", user_id=user.id, contact_id=contact_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    row = notes_repo.upsert(db, user.id, contact_id, text)
    db.commit()
    db.refresh(row)
    log.info("contacts.my_note_upserted", user_id=user.id, contact_id=contact_id)
    return MyContactNoteOut.model_validate(row)


@router.delete("/{contact_id}/my-notes", response_model=Ok)
def delete_my_note(contact_id: int, user: CurrentUser, db: DbDep) -> Ok:
    """Explicit delete. 404 if no note exists for (user, contact)."""
    _require_contact_exists(db, contact_id)
    was_deleted = notes_repo.delete_row(db, user.id, contact_id)
    if not was_deleted:
        raise ApiError(
            "not_found",
            "You haven't added a note for this contact yet.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    db.commit()
    log.info("contacts.my_note_deleted", user_id=user.id, contact_id=contact_id)
    return Ok()
