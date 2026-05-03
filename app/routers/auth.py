"""Auth endpoints.

Two flavours of route under different mount points:

  * Browser-redirect bootstrap (cannot send custom headers):
      GET  /auth/login              → 302 to Google
      GET  /auth/google/callback    → finalize, set cookie, 302 to frontend

  * JSON API (require X-Requested-With + valid session cookie):
      POST /api/v1/auth/logout
      POST /api/v1/auth/disconnect
      GET  /api/v1/auth/me
"""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.config import settings
from app.core.cookies import (
    OAUTH_STATE_COOKIE,
    clear_oauth_state_cookie,
    clear_session_cookie,
    set_oauth_state_cookie,
    set_session_cookie,
)
from app.core.crypto import decrypt_optional
from app.core.deps import SESSION_COOKIE, CurrentUser, DbDep
from app.repositories import sessions as sessions_repo
from app.schemas.auth import MeOut
from app.schemas.common import Ok
from app.services import auth as auth_service
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


# ─────────────────────────── browser bootstrap ───────────────────────────


@bootstrap.get("/login")
def login() -> RedirectResponse:
    try:
        auth_url, state = build_authorization_url(settings.GOOGLE_REDIRECT_URI)
    except OAuthError as e:
        return _frontend_redirect("/connect", error=str(e))

    resp = RedirectResponse(url=auth_url, status_code=status.HTTP_302_FOUND)
    set_oauth_state_cookie(resp, state)
    return resp


@bootstrap.get("/google/callback")
def google_callback(
    request: Request,
    db: DbDep,
    code: str = Query(...),
    state: str = Query(...),
    state_cookie: str | None = Cookie(default=None, alias=OAUTH_STATE_COOKIE),
) -> RedirectResponse:
    if not state_cookie or state_cookie != state:
        return _frontend_redirect("/connect", error="state_mismatch")

    try:
        identity = exchange_code(code, state, settings.GOOGLE_REDIRECT_URI)
    except OAuthError as e:
        resp = _frontend_redirect("/connect", error=str(e))
        clear_oauth_state_cookie(resp)
        return resp

    user, session = auth_service.complete_google_login(
        db,
        identity,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )

    redirect_to = "/onboarding" if not user.is_onboarded else "/dashboard"
    resp = _frontend_redirect(redirect_to)
    set_session_cookie(
        resp,
        session.id,
        max_age_seconds=int(timedelta(days=settings.SESSION_TTL_DAYS).total_seconds()),
    )
    clear_oauth_state_cookie(resp)
    return resp


# ─────────────────────────── JSON API ───────────────────────────


@api.get("/me", response_model=MeOut)
def me(user: CurrentUser) -> MeOut:
    return MeOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        is_admin=user.is_admin,
        onboarded=user.is_onboarded,
        has_gmail_connected=user.has_gmail_connected,
        daily_limit=user.daily_limit,
        sent_today=user.sent_today,
    )


@api.post("/logout", response_model=Ok)
def logout(
    db: DbDep,
    response: Response,
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> Ok:
    if session_cookie:
        sessions_repo.delete_by_id(db, session_cookie)
        db.commit()
    clear_session_cookie(response)
    return Ok()


@api.post("/disconnect", response_model=Ok)
def disconnect(user: CurrentUser, db: DbDep, response: Response) -> Ok:
    refresh_token = decrypt_optional(user.google_refresh_token)
    if refresh_token:
        revoke_refresh_token(refresh_token)

    user.google_refresh_token = None
    user.google_access_token = None
    user.google_token_expiry = None
    user.google_scopes = None
    user.google_connected_at = None
    db.add(user)

    sessions_repo.delete_by_user(db, user.id)
    db.commit()
    clear_session_cookie(response)
    return Ok()
