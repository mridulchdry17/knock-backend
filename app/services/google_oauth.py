"""Google OAuth 2.0 flow — strictly the auth handshake.

Gmail send adapter lives in services/gmail_send.py (B5.5). This module also
owns the credentials lookup (`get_user_credentials`) used by the send worker —
it's the cleanest home since we already encrypt/decrypt tokens here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from app.config import settings
from app.core.crypto import decrypt_optional
from app.models import User

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


def build_authorization_url(redirect_uri: str) -> tuple[str, str, str]:
    """Return (authorization_url, state, code_verifier).

    State + code_verifier must both be persisted (cookies) and re-supplied on
    callback. Google's OAuth server now requires PKCE on the token-exchange
    leg even for confidential web clients — without `code_verifier` the
    exchange fails with `invalid_grant: Missing code verifier`.
    """
    flow = _build_flow(redirect_uri)
    flow.autogenerate_code_verifier = True
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return auth_url, state, flow.code_verifier


def exchange_code(
    code: str, state: str, redirect_uri: str, code_verifier: str | None = None
) -> GoogleIdentity:
    """Exchange the OAuth code for tokens and identity. Validates required scopes
    and verifies the id_token. Returns a frozen GoogleIdentity."""
    flow = _build_flow(redirect_uri, state=state)
    if code_verifier:
        flow.code_verifier = code_verifier
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


def get_user_credentials(user: User) -> Credentials:
    """Build a google.oauth2 Credentials object from the user's stored tokens.

    Decrypts refresh + access tokens via Fernet and constructs a Credentials
    object the Gmail API client can use directly. google-auth handles refresh
    automatically when an access_token is expired — but we don't persist the
    refreshed token back here in v0 (B5.5 doesn't need that — Credentials.refresh()
    mutates the in-memory object and Gmail calls just work). When B5.6 ships
    long-lived workers, lift the post-refresh persist into a service helper.
    """
    refresh_token = decrypt_optional(user.google_refresh_token)
    if not refresh_token:
        raise OAuthError("missing_refresh_token")

    access_token = decrypt_optional(user.google_access_token)

    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=list(SCOPES),
    )


def revoke_refresh_token(refresh_token: str) -> None:
    """Best-effort revoke at Google's revoke endpoint. Failures are swallowed —
    we always clear our local copy regardless."""
    import contextlib

    import httpx

    with contextlib.suppress(httpx.HTTPError):
        httpx.post(
            "https://oauth2.googleapis.com/revoke",
            data={"token": refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=5.0,
        )
