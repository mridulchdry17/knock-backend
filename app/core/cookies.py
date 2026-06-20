"""Cookie helpers — OAuth round-trip cookies + the refresh-token cookie.

Access-token transport is `Authorization: Bearer <token>` (see core/deps.py).
The refresh token rides in an HttpOnly cookie because JS must never read it —
that's the whole point of the two-token model.

The oauth_state cookie binds the OAuth round-trip (login → Google → callback)
and the oauth_code_verifier cookie carries the PKCE verifier across the same
two endpoints.
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


# ─────────────────────────── refresh-token cookie ───────────────────────────

REFRESH_TOKEN_COOKIE = "refresh_token"


def set_refresh_token_cookie(resp: Response, raw_token: str) -> None:
    """Set the long-lived HttpOnly refresh-token cookie.

    Attributes:
      - HttpOnly: JS can never read it (the whole point of this model)
      - Secure: HTTPS only — except in local dev (COOKIE_SECURE=False)
      - SameSite=Lax: CSRF protection. Lax (not Strict) so the OAuth callback's
        cross-site redirect from Google can still set the cookie. Lax permits
        top-level navigations and same-site fetches — both of which we need.
      - Path=/: sent on every API call. Backend ignores it on non-refresh
        endpoints; httpOnly means JS can't exfiltrate either way.
      - Max-Age: REFRESH_TOKEN_TTL_DAYS converted to seconds.
    """
    resp.set_cookie(
        key=REFRESH_TOKEN_COOKIE,
        value=raw_token,
        max_age=settings.REFRESH_TOKEN_TTL_DAYS * 24 * 3600,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        domain=_domain(),
        path="/",
    )


def clear_refresh_token_cookie(resp: Response) -> None:
    resp.delete_cookie(key=REFRESH_TOKEN_COOKIE, domain=_domain(), path="/")
