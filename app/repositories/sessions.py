from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session as OrmSession

from app.core.time import utcnow
from app.models import Session as SessionRow


def get_active(db: OrmSession, token: str) -> SessionRow | None:
    """Return the session iff it exists and has not expired."""
    row = db.get(SessionRow, token)
    if row is None or row.expires_at <= utcnow():
        return None
    return row


def add(db: OrmSession, session: SessionRow) -> SessionRow:
    db.add(session)
    db.flush()
    return session


def touch(db: OrmSession, session: SessionRow) -> None:
    """Sliding-window: bump last_used_at on each authenticated request."""
    session.last_used_at = utcnow()
    db.add(session)


def delete_by_id(db: OrmSession, token: str) -> None:
    db.execute(delete(SessionRow).where(SessionRow.id == token))


def delete_by_user(db: OrmSession, user_id: int) -> None:
    db.execute(delete(SessionRow).where(SessionRow.user_id == user_id))
