"""Issue server-side opaque session tokens (per PRD §16).

CRUD lives in repositories.sessions; this module owns the one compound
operation: generate a fresh token + persist a row. Callers continue to
own the commit boundary.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy.orm import Session as OrmSession

from app.config import settings
from app.core.time import utcnow
from app.models import Session as SessionRow
from app.repositories import sessions as sessions_repo

_TOKEN_BYTES = 32  # 256 bits → 43-char urlsafe-b64 string


def issue(
    db: OrmSession,
    *,
    user_id: int,
    user_agent: str | None = None,
    ip: str | None = None,
) -> SessionRow:
    """Mint the short-lived access token (= sessions row).

    TTL is `ACCESS_TOKEN_TTL_MINUTES` (15 min by default). The browser holds
    this in memory only; when it expires, the frontend silent-refreshes via
    the HttpOnly refresh cookie to mint a new one. See
    `app.services.refresh_tokens` for the long-lived half.
    """
    row = SessionRow(
        id=secrets.token_urlsafe(_TOKEN_BYTES),
        user_id=user_id,
        expires_at=utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_TTL_MINUTES),
        user_agent=user_agent,
        ip=ip,
    )
    return sessions_repo.add(db, row)
