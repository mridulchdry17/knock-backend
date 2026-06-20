"""Auth endpoints.

Two flavours of route under different mount points:

  * Browser-redirect bootstrap (cannot send custom headers):
      GET  /auth/login              → 302 to Google
      GET  /auth/google/callback    → finalize, 302 to frontend with token in URL fragment

  * JSON API (require `Authorization: Bearer <token>`):
      POST /api/v1/auth/logout
      POST /api/v1/auth/disconnect
      GET  /api/v1/auth/me

Session transport is the bearer token (raw `sessions.id`). The token is delivered
to the frontend via URL fragment (`#token=...`) on the OAuth callback redirect —
fragments never reach the server, so they don't appear in access logs or referer
headers. The frontend stores the token in `sessionStorage` and attaches it as
`Authorization: Bearer <token>` on every API call.
"""
from __future__ import annotations

from urllib.parse import quote, urlencode

from fastapi import APIRouter, Cookie, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.config import settings
from app.core.cookies import (
    OAUTH_CODE_VERIFIER_COOKIE,
    OAUTH_STATE_COOKIE,
    REFRESH_TOKEN_COOKIE,
    clear_oauth_code_verifier_cookie,
    clear_oauth_state_cookie,
    clear_refresh_token_cookie,
    set_oauth_code_verifier_cookie,
    set_oauth_state_cookie,
    set_refresh_token_cookie,
)
from app.core.crypto import decrypt_optional
from app.core.deps import CurrentSessionToken, CurrentUser, DbDep
from app.core.errors import ApiError
from app.repositories import refresh_tokens as refresh_tokens_repo
from app.repositories import sessions as sessions_repo
from app.schemas.auth import MeOut, RefreshOut
from app.schemas.common import Ok
from app.services import auth as auth_service
from app.services import refresh_tokens as refresh_tokens_service
from app.services import sessions as sessions_service
from app.services.google_oauth import (
    OAuthError,
    build_authorization_url,
    exchange_code,
    revoke_refresh_token,
)

bootstrap = APIRouter(prefix="/auth", tags=["auth"])
api = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _frontend_redirect(path: str, **params: str) -> RedirectResponse:
    base = settings.FRONTEND_ORIGIN.rstrip("/") + path
    if params:
        base = f"{base}?{urlencode(params)}"
    return RedirectResponse(url=base, status_code=status.HTTP_302_FOUND)


def _frontend_redirect_with_token(path: str, token: str, **params: str) -> RedirectResponse:
    """Redirect to frontend with the session token in the URL fragment.

    The fragment is browser-only — it never reaches the backend, doesn't appear
    in access logs, and is stripped from `Referer` headers. Frontend's
    `/auth/complete` page reads the fragment, stores the token, and clears the
    URL via `history.replaceState`.
    """
    base = settings.FRONTEND_ORIGIN.rstrip("/") + path
    if params:
        base = f"{base}?{urlencode(params)}"
    base = f"{base}#token={quote(token, safe='')}"
    return RedirectResponse(url=base, status_code=status.HTTP_302_FOUND)


# ─────────────────────────── browser bootstrap ───────────────────────────


@bootstrap.get("/login")
def login() -> RedirectResponse:
    try:
        auth_url, state, code_verifier = build_authorization_url(settings.GOOGLE_REDIRECT_URI)
    except OAuthError as e:
        return _frontend_redirect("/connect", error=str(e))

    resp = RedirectResponse(url=auth_url, status_code=status.HTTP_302_FOUND)
    set_oauth_state_cookie(resp, state)
    set_oauth_code_verifier_cookie(resp, code_verifier)
    return resp


@bootstrap.get("/google/callback")
def google_callback(
    request: Request,
    db: DbDep,
    code: str = Query(...),
    state: str = Query(...),
    state_cookie: str | None = Cookie(default=None, alias=OAUTH_STATE_COOKIE),
    code_verifier_cookie: str | None = Cookie(default=None, alias=OAUTH_CODE_VERIFIER_COOKIE),
) -> RedirectResponse:
    if not state_cookie or state_cookie != state:
        return _frontend_redirect("/connect", error="state_mismatch")

    try:
        identity = exchange_code(
            code, state, settings.GOOGLE_REDIRECT_URI, code_verifier=code_verifier_cookie
        )
    except OAuthError as e:
        resp = _frontend_redirect("/connect", error=str(e))
        clear_oauth_state_cookie(resp)
        clear_oauth_code_verifier_cookie(resp)
        return resp

    _user, session, refresh, decision = auth_service.complete_google_login(
        db,
        identity,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )

    resp = _frontend_redirect_with_token("/auth/complete", session.id, next=decision.next_path)
    # Long-lived refresh token rides as an HttpOnly cookie on the same response —
    # the access token (short-lived) goes in the fragment for the frontend to
    # consume immediately; the refresh cookie is used for silent re-issuance
    # whenever the access token expires.
    set_refresh_token_cookie(resp, refresh.raw_token)
    clear_oauth_state_cookie(resp)
    clear_oauth_code_verifier_cookie(resp)
    return resp


# ─────────────────────────── JSON API ───────────────────────────


@api.get("/me", response_model=MeOut)
def me(user: CurrentUser) -> MeOut:
    return MeOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        tier=user.tier,
        onboarded=user.is_onboarded,
        has_gmail_connected=user.has_gmail_connected,
        daily_limit=user.daily_limit,
        sent_today=user.sent_today,
    )


@api.post("/refresh", response_model=RefreshOut)
def refresh(
    request: Request,
    response: Response,
    db: DbDep,
    refresh_token_cookie: str | None = Cookie(default=None, alias=REFRESH_TOKEN_COOKIE),
) -> RefreshOut:
    """Silent re-issuance. Validates the HttpOnly refresh cookie, rotates it
    (mints a new refresh token in the same family + revokes the presented
    one), and issues a fresh short-lived access token.

    No `Authorization` header required — auth is purely via the HttpOnly
    cookie. This is the only endpoint where the refresh token is consumed.

    Returns 401 on:
      - missing cookie
      - expired or already-revoked token (gentle: clear cookie + 401)
      - reuse detected (loud: family revoked + 401; both legit device and
        attacker lose access until next interactive login)
    """
    if not refresh_token_cookie:
        raise ApiError(
            "no_refresh_token",
            "Not authenticated.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    result = refresh_tokens_service.validate_and_rotate(
        db,
        raw_token=refresh_token_cookie,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )

    if result.invalid or result.reuse_detected:
        # Clear the dead cookie so the browser stops sending it on every API
        # call. 401 lets the frontend route the user back to the login screen.
        clear_refresh_token_cookie(response)
        db.commit()  # the family-revocation write needs to land
        code = "refresh_reuse_detected" if result.reuse_detected else "refresh_invalid"
        raise ApiError(code, "Session expired.", status_code=status.HTTP_401_UNAUTHORIZED)

    assert result.rotated is not None  # guaranteed by the validate contract

    # Mint a fresh access token (15-min TTL) for this rotation. The session
    # row IS the access token (see sessions.py); the refresh chain and the
    # access token are intentionally independent — losing one doesn't expose
    # the other to compromise.
    access_session = sessions_service.issue(
        db,
        user_id=result.user_id,  # type: ignore[arg-type]  -- always set on `rotated`
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    db.commit()

    # Refresh cookie rotates on every successful refresh — set the new one
    # on the response. The old cookie is overwritten on the browser side.
    set_refresh_token_cookie(response, result.rotated.raw_token)
    return RefreshOut(access_token=access_session.id)


@api.post("/logout", response_model=Ok)
def logout(
    response: Response,
    db: DbDep,
    token: CurrentSessionToken,
    refresh_token_cookie: str | None = Cookie(default=None, alias=REFRESH_TOKEN_COOKIE),
) -> Ok:
    """Sign-out: revoke the access token (sessions row), revoke the refresh
    family (every device sharing this family is logged out), and clear the
    refresh cookie. Idempotent — re-calling on an already-logged-out session
    just confirms a no-op state."""
    sessions_repo.delete_by_id(db, token)
    if refresh_token_cookie:
        refresh_tokens_service.revoke_family_for_token(db, refresh_token_cookie)
    db.commit()
    clear_refresh_token_cookie(response)
    return Ok()


@api.post("/disconnect", response_model=Ok)
def disconnect(user: CurrentUser, db: DbDep, response: Response) -> Ok:
    google_refresh_token = decrypt_optional(user.google_refresh_token)
    if google_refresh_token:
        revoke_refresh_token(google_refresh_token)

    user.google_refresh_token = None
    user.google_access_token = None
    user.google_token_expiry = None
    user.google_scopes = None
    user.google_connected_at = None
    db.add(user)

    # Disconnect implies full sign-out across devices: nuke every access token
    # session AND every refresh token for this user. The HttpOnly refresh
    # cookie on the current device is cleared in the response so the browser
    # stops sending a now-orphaned secret on subsequent calls.
    sessions_repo.delete_by_user(db, user.id)
    refresh_tokens_repo.revoke_all_for_user(db, user.id)
    db.commit()
    clear_refresh_token_cookie(response)
    return Ok()
