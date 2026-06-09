"""Per-user contact notes repository.

Owns SQL for the `user_contact_notes` table. Service/router layer enforces
ownership; this module just translates IDs to rows. Mirrors the lookup style
of `preferences.py` (composite-PK `db.get()`).
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as OrmSession

from app.models import UserContactNote


def get(db: OrmSession, user_id: int, contact_id: int) -> UserContactNote | None:
    return db.get(UserContactNote, (user_id, contact_id))


def upsert(
    db: OrmSession, user_id: int, contact_id: int, notes: str
) -> UserContactNote:
    """Insert or update the user's note for this contact. Caller commits.

    Idempotent: same notes value on a second call updates `updated_at` but
    is otherwise a no-op. Empty-string handling is the router's job — this
    function never deletes.
    """
    existing = get(db, user_id, contact_id)
    if existing is not None:
        existing.notes = notes
        db.add(existing)
        db.flush()
        return existing
    row = UserContactNote(user_id=user_id, contact_id=contact_id, notes=notes)
    db.add(row)
    db.flush()
    return row


def delete_row(db: OrmSession, user_id: int, contact_id: int) -> bool:
    """Returns True if a row was deleted, False if no note existed."""
    result = db.execute(
        delete(UserContactNote).where(
            UserContactNote.user_id == user_id,
            UserContactNote.contact_id == contact_id,
        )
    )
    return bool(result.rowcount)


def list_by_user(db: OrmSession, user_id: int) -> list[UserContactNote]:
    """All notes the user has authored. Newest first.

    Not used by B5.1b endpoints; reserved for a future 'My research' surface
    (out of scope for this slice).
    """
    return list(
        db.scalars(
            select(UserContactNote)
            .where(UserContactNote.user_id == user_id)
            .order_by(UserContactNote.updated_at.desc())
        ).all()
    )
