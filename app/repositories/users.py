from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.models import User


def get(db: OrmSession, user_id: int) -> User | None:
    return db.get(User, user_id)


def get_by_email(db: OrmSession, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email.lower()))


def get_by_google_sub(db: OrmSession, sub: str) -> User | None:
    return db.scalar(select(User).where(User.google_sub == sub))


def add(db: OrmSession, user: User) -> User:
    db.add(user)
    db.flush()
    return user
