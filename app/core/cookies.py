"""Cookie helpers — single source of truth for session/oauth-state cookie attrs."""
from __future__ import annotations

from fastapi import Response

from app.config import settings
from app.core.deps import SESSION_COOKIE

OAUTH_STATE_COOKIE = "oauth_state"
_OAUTH_STATE_TTL_SECONDS = 600  # 10 minutes; user has to finish the round-trip in time


def _domain() -> str | None:
    return settings.COOKIE_DOMAIN or None


def set_session_cookie(resp: Response, token: str, *, max_age_seconds: int) -> None:
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=max_age_seconds,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        domain=_domain(),
        path="/",
    )


def clear_session_cookie(resp: Response) -> None:
    resp.delete_cookie(
        key=SESSION_COOKIE,
        domain=_domain(),
        path="/",
    )


def set_oauth_state_cookie(resp: Response, state: str) -> None:
    resp.set_cookie(
        key=OAUTH_STATE_COOKIE,
        value=state,
        max_age=_OAUTH_STATE_TTL_SECONDS,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",  # always Lax — top-level redirect from Google
        domain=_domain(),
        path="/",
    )


def clear_oauth_state_cookie(resp: Response) -> None:
    resp.delete_cookie(key=OAUTH_STATE_COOKIE, domain=_domain(), path="/")
