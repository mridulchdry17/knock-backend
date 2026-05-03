from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends, status
from sqlalchemy.orm import Session as OrmSession

from app.core.errors import ApiError
from app.core.time import utcnow
from app.db.session import get_db
from app.models import Session as SessionRow
from app.models import User

SESSION_COOKIE = "session"

DbDep = Annotated[OrmSession, Depends(get_db)]


def _load_session(db: OrmSession, token: str) -> SessionRow | None:
    row = db.get(SessionRow, token)
    if row is None or row.expires_at <= utcnow():
        return None
    return row


def get_current_user(
    db: DbDep,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User:
    if not session_cookie:
        raise ApiError("unauthorized", "Not authenticated", status_code=status.HTTP_401_UNAUTHORIZED)
    session = _load_session(db, session_cookie)
    if session is None:
        raise ApiError("unauthorized", "Session expired", status_code=status.HTTP_401_UNAUTHORIZED)

    user = db.get(User, session.user_id)
    if user is None:
        raise ApiError("unauthorized", "User not found", status_code=status.HTTP_401_UNAUTHORIZED)
    if user.is_suspended:
        raise ApiError("account_suspended", "Account is suspended", status_code=status.HTTP_403_FORBIDDEN)

    session.last_used_at = utcnow()
    db.add(session)
    db.commit()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise ApiError("forbidden", "Admin only", status_code=status.HTTP_403_FORBIDDEN)
    return user


AdminUser = Annotated[User, Depends(require_admin)]
