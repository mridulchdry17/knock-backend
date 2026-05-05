"""Test fixtures.

Each test gets a fresh in-memory SQLite database with the schema created via
SQLAlchemy's metadata (skipping alembic for speed). Migrations are smoke-tested
separately in CI via `alembic upgrade head`.

Why not share an engine across tests: in-memory SQLite is per-connection,
and we want strong isolation. Per-test engines cost ~10ms each; cheap enough.
"""
from __future__ import annotations

# Set env vars BEFORE importing app modules — settings is lru_cached on import.
import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault(
    "TOKEN_ENCRYPTION_KEY",
    "ZmFrZS1mZXJuZXQta2V5LWZvci10ZXN0cy1vbmx5MzJiPT0=",  # fake, base64 32-byte
)
os.environ.setdefault("SUPER_ADMIN_EMAILS", "")

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base

# Import all models so Base.metadata knows about them.
from app.models import (  # noqa: F401
    Campaign,
    Company,
    Contact,
    EmailLog,
    GlobalContactLock,
    SendQueue,
    Template,
    User,
    UserContactMap,
    WaitlistEntry,
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    # StaticPool keeps a single underlying connection across all sessions, which
    # is what we need for sqlite:///:memory: — otherwise every fresh checkout
    # gets a brand-new empty database.
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def waitlist_email(db: Session) -> str:
    """Seeds one waitlist entry; returns the email."""
    from app.repositories import waitlist as waitlist_repo

    email = "founder@startup.com"
    waitlist_repo.add(db, email)
    db.commit()
    return email


def _make_user(
    db: Session,
    *,
    email: str,
    google_sub: str = "g-sub-1",
    tier: str = "pending",
    waitlist_email: str | None = None,
) -> User:
    user = User(
        email=email,
        google_sub=google_sub,
        tier=tier,
        waitlist_email=waitlist_email,
    )
    db.add(user)
    db.commit()
    return user
