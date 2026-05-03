"""Auth orchestration: complete an OAuth round-trip → user row + session row.

Routers should call `complete_google_login` and never touch the DB / crypto
themselves. Keeps the cookie-setting concern in the router and the persistence
concern here.
"""
from __future__ import annotations

from sqlalchemy.orm import Session as OrmSession

from app.config import settings
from app.core.crypto import encrypt
from app.core.time import utcnow
from app.models import Session as SessionRow
from app.models import User
from app.repositories import users as users_repo
from app.services import sessions as sessions_service
from app.services.google_oauth import GoogleIdentity


def _is_admin_email(email: str) -> bool:
    return email.lower() in settings.admin_emails_set


def _upsert_user(db: OrmSession, identity: GoogleIdentity) -> User:
    user = users_repo.get_by_google_sub(db, identity.sub)
    if user is None:
        user = users_repo.get_by_email(db, identity.email)
    if user is None:
        user = users_repo.add(
            db,
            User(
                email=identity.email.lower(),
                full_name=identity.full_name,
                google_sub=identity.sub,
                is_admin=_is_admin_email(identity.email),
            ),
        )

    user.email = identity.email.lower()
    user.full_name = identity.full_name or user.full_name
    user.google_sub = identity.sub
    user.google_refresh_token = encrypt(identity.refresh_token)
    user.google_access_token = encrypt(identity.access_token)
    user.google_token_expiry = identity.token_expiry
    user.google_scopes = " ".join(sorted(identity.granted_scopes))
    user.google_connected_at = utcnow()
    if _is_admin_email(identity.email):
        user.is_admin = True
    db.flush()
    return user


def complete_google_login(
    db: OrmSession,
    identity: GoogleIdentity,
    *,
    user_agent: str | None,
    ip: str | None,
) -> tuple[User, SessionRow]:
    user = _upsert_user(db, identity)
    session = sessions_service.issue(db, user_id=user.id, user_agent=user_agent, ip=ip)
    db.commit()
    return user, session
