"""Contacts router — user-facing endpoints.

B5.1b ships just the per-user notes endpoints (`/{contact_id}/my-notes`).
B5.3 will add the read-only contact browser (list/detail) on this same router.

Layered: router validates payload + ownership, repositories own SQL. No
service layer yet — the surface is too thin to earn one. When B5.3 lands
list/detail with filtering, factor a service if logic crosses the third
occurrence threshold.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.core.deps import CurrentUser, DbDep, require_tier
from app.core.errors import ApiError
from app.logging_config import get_logger
from app.repositories import contacts as contacts_repo
from app.repositories import user_contact_notes as notes_repo
from app.schemas.common import Ok
from app.schemas.contacts import MyContactNoteIn, MyContactNoteOut

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
