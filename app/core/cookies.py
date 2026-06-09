"""Cookie helpers — only the OAuth round-trip cookies remain.

Session transport is now `Authorization: Bearer <token>` (see core/deps.py).
The oauth_state cookie binds the OAuth round-trip (login → Google → callback)
and the oauth_code_verifier cookie carries the PKCE verifier across the same
two endpoints. Both endpoints live on the backend domain, so cross-domain
cookie sharing is not a concern here.
"""
from __future__ import annotations

from fastapi import Response

from app.config import settings

OAUTH_STATE_COOKIE = "oauth_state"
OAUTH_CODE_VERIFIER_COOKIE = "oauth_code_verifier"
_OAUTH_TTL_SECONDS = 600  # 10 minutes; user has to finish the round-trip in time


def _domain() -> str | None:
    return settings.COOKIE_DOMAIN or None


def _set_round_trip_cookie(resp: Response, key: str, value: str) -> None:
    resp.set_cookie(
        key=key,
        value=value,
        max_age=_OAUTH_TTL_SECONDS,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",  # always Lax — top-level redirect from Google
        domain=_domain(),
        path="/",
    )


def set_oauth_state_cookie(resp: Response, state: str) -> None:
    _set_round_trip_cookie(resp, OAUTH_STATE_COOKIE, state)


def clear_oauth_state_cookie(resp: Response) -> None:
    resp.delete_cookie(key=OAUTH_STATE_COOKIE, domain=_domain(), path="/")


def set_oauth_code_verifier_cookie(resp: Response, code_verifier: str) -> None:
    _set_round_trip_cookie(resp, OAUTH_CODE_VERIFIER_COOKIE, code_verifier)


def clear_oauth_code_verifier_cookie(resp: Response) -> None:
    resp.delete_cookie(key=OAUTH_CODE_VERIFIER_COOKIE, domain=_domain(), path="/")
