"""Google OAuth 2.0 flow — strictly the auth handshake.

Gmail API send/read lives in services/gmail.py (added in a later phase).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow

from app.config import settings

SCOPES: list[str] = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]
REQUIRED_GMAIL_SCOPES: frozenset[str] = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    }
)

# Allow http://localhost during dev — google-auth-oauthlib otherwise rejects non-HTTPS.
if not settings.is_prod:
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    sub: str
    email: str
    full_name: str | None
    picture: str | None
    refresh_token: str
    access_token: str
    token_expiry: datetime
    granted_scopes: frozenset[str]


class OAuthError(RuntimeError):
    """Raised for any OAuth failure surfaced to the user."""


def _build_flow(redirect_uri: str, state: str | None = None) -> Flow:
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise OAuthError("google_oauth_unconfigured")

    flow = Flow.from_client_config(
        client_config={
            "web": {
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        state=state,
    )
    flow.redirect_uri = redirect_uri
    return flow


def build_authorization_url(redirect_uri: str) -> tuple[str, str]:
    """Return (authorization_url, state). State must be persisted (cookie) and
    re-verified on callback."""
    flow = _build_flow(redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return auth_url, state


def exchange_code(code: str, state: str, redirect_uri: str) -> GoogleIdentity:
    """Exchange the OAuth code for tokens and identity. Validates required scopes
    and verifies the id_token. Returns a frozen GoogleIdentity."""
    flow = _build_flow(redirect_uri, state=state)
    flow.fetch_token(code=code)

    creds = flow.credentials
    if not creds.refresh_token:
        # We force prompt=consent → Google should return one. If missing, the user
        # likely went through a partial flow. Bail loudly rather than persist garbage.
        raise OAuthError("missing_refresh_token")

    granted = frozenset(creds.scopes or [])
    if not REQUIRED_GMAIL_SCOPES.issubset(granted):
        raise OAuthError("missing_required_scopes")

    if not creds.id_token:
        raise OAuthError("missing_id_token")

    info = id_token.verify_oauth2_token(
        creds.id_token,
        GoogleAuthRequest(),
        audience=settings.GOOGLE_CLIENT_ID,
        clock_skew_in_seconds=60,
    )

    sub = info.get("sub")
    email = info.get("email")
    if not sub or not email:
        raise OAuthError("invalid_id_token")

    if creds.expiry is None:
        raise OAuthError("missing_token_expiry")

    # google-auth returns a naive UTC datetime; we store tz-aware everywhere.
    expiry = creds.expiry.replace(tzinfo=UTC) if creds.expiry.tzinfo is None else creds.expiry

    return GoogleIdentity(
        sub=sub,
        email=email,
        full_name=info.get("name"),
        picture=info.get("picture"),
        refresh_token=creds.refresh_token,
        access_token=creds.token,
        token_expiry=expiry,
        granted_scopes=granted,
    )


def revoke_refresh_token(refresh_token: str) -> None:
    """Best-effort revoke at Google's revoke endpoint. Failures are swallowed —
    we always clear our local copy regardless."""
    import httpx

    try:
        httpx.post(
            "https://oauth2.googleapis.com/revoke",
            data={"token": refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=5.0,
        )
    except httpx.HTTPError:
        pass
