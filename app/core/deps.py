from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends, status
from sqlalchemy.orm import Session as OrmSession

from app.core.errors import ApiError
from app.db.session import get_db
from app.models import User
from app.repositories import sessions as sessions_repo
from app.repositories import users as users_repo

SESSION_COOKIE = "session"

DbDep = Annotated[OrmSession, Depends(get_db)]


def get_current_user(
    db: DbDep,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User:
    if not session_cookie:
        raise ApiError("unauthorized", "Not authenticated", status_code=status.HTTP_401_UNAUTHORIZED)
    session = sessions_repo.get_active(db, session_cookie)
    if session is None:
        raise ApiError("unauthorized", "Session expired", status_code=status.HTTP_401_UNAUTHORIZED)

    user = users_repo.get(db, session.user_id)
    if user is None:
        raise ApiError("unauthorized", "User not found", status_code=status.HTTP_401_UNAUTHORIZED)
    if user.is_suspended:
        raise ApiError(
            "account_suspended", "Account is suspended", status_code=status.HTTP_403_FORBIDDEN
        )

    sessions_repo.touch(db, session)
    db.commit()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise ApiError("forbidden", "Admin only", status_code=status.HTTP_403_FORBIDDEN)
    return user


AdminUser = Annotated[User, Depends(require_admin)]
