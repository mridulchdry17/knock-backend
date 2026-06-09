from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session as OrmSession

from app.core.errors import ApiError
from app.core.time import ensure_utc, utcnow
from app.db.session import get_db
from app.logging_config import get_logger
from app.models import User
from app.repositories import sessions as sessions_repo
from app.repositories import users as users_repo

log = get_logger("deps")

# Only refresh a session's last_used_at if it's older than this. The field is
# coarse "last seen" activity tracking — writing it on EVERY request put a
# remote DB write (~150-250ms) on the hot path of every authenticated call.
# Throttling collapses that to at most one write per session per window.
SESSION_TOUCH_THROTTLE = timedelta(minutes=15)

DbDep = Annotated[OrmSession, Depends(get_db)]

# auto_error=False so we can raise ApiError ourselves with a consistent envelope
# instead of FastAPI's default 403 plain JSON.
_bearer_scheme = HTTPBearer(auto_error=False)


def _extract_token(creds: HTTPAuthorizationCredentials | None) -> str:
    if creds is None or not creds.credentials:
        raise ApiError(
            "unauthorized", "Not authenticated", status_code=status.HTTP_401_UNAUTHORIZED
        )
    return creds.credentials


def get_current_session_token(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> str:
    """The raw bearer token from the Authorization header.

    Used by logout/disconnect to identify which session to revoke without
    re-querying the DB. Other callers should depend on `CurrentUser` instead.
    """
    return _extract_token(creds)


CurrentSessionToken = Annotated[str, Depends(get_current_session_token)]


def get_current_user(
    db: DbDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> User:
    token = _extract_token(creds)
    session = sessions_repo.get_active(db, token)
    if session is None:
        raise ApiError(
            "unauthorized", "Session expired", status_code=status.HTTP_401_UNAUTHORIZED
        )

    user = users_repo.get(db, session.user_id)
    if user is None:
        raise ApiError(
            "unauthorized", "User not found", status_code=status.HTTP_401_UNAUTHORIZED
        )
    if user.is_suspended:
        raise ApiError(
            "account_suspended", "Account is suspended", status_code=status.HTTP_403_FORBIDDEN
        )

    # Throttled last-seen update. Writing last_used_at on every request put a
    # remote write on the hot path of every authenticated call; we only refresh
    # it once the stored value is older than SESSION_TOUCH_THROTTLE. Still
    # best-effort: a Turso stream hiccup must not fail the request.
    last_used = ensure_utc(session.last_used_at) if session.last_used_at else None
    if last_used is None or (utcnow() - last_used) > SESSION_TOUCH_THROTTLE:
        try:
            sessions_repo.touch(db, session)
            db.commit()
        except Exception as exc:  # pragma: no cover — Turso transport flake
            log.warning("session.touch_failed", error=str(exc), session_id=session.id)
            db.rollback()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


# ─────────────────────────── tier gating ───────────────────────────


def require_tier(*allowed: str) -> Callable[[User], User]:
    """Factory: returns a dependency that 403s if `current_user.tier` not in allowed.

    Usage:
        @router.post("/foo", dependencies=[Depends(require_tier("free", "paid"))])
        ...

    Or for an annotated alias:
        require_paid = require_tier("paid", "super_admin")

    The 'pending' tier is intentionally never in any allowed list — pending
    users see 403 on every feature route until super_admin approves them.
    """
    allowed_set = frozenset(allowed)

    def dep(user: CurrentUser) -> User:
        if user.tier not in allowed_set:
            raise ApiError(
                "forbidden",
                f"This action requires tier in {sorted(allowed_set)}; your tier is '{user.tier}'.",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        return user

    return dep


# Common tier gates. Add new ones only when a route actually needs them.
require_super_admin = require_tier("super_admin")
require_paid = require_tier("paid", "super_admin")  # super_admin inherits paid access

SuperAdminUser = Annotated[User, Depends(require_super_admin)]
PaidUser = Annotated[User, Depends(require_paid)]
