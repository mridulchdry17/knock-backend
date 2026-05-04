"""Cookie helpers — only the OAuth-state cookie remains.

Session transport is now `Authorization: Bearer <token>` (see core/deps.py).
The oauth_state cookie is a same-origin, 10-minute Lax cookie used purely to
bind the OAuth round-trip (login → Google → callback). Both endpoints live on
the backend domain, so cross-domain cookie sharing is not a concern here.
"""
from __future__ import annotations

from fastapi import Response

from app.config import settings

OAUTH_STATE_COOKIE = "oauth_state"
_OAUTH_STATE_TTL_SECONDS = 600  # 10 minutes; user has to finish the round-trip in time


def _domain() -> str | None:
    return settings.COOKIE_DOMAIN or None


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
