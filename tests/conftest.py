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
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base

# Import all models so Base.metadata knows about them.
from app.models import (  # noqa: F401
    Campaign,
    Company,
    Contact,
    EmailFailure,
    EmailLog,
    GlobalContactLock,
    PlatformCompanyLock,
    SendQueue,
    Template,
    TodayBatchItem,
    User,
    UserCompanyLock,
    UserContactMap,
    UserContactNote,
    UserExcludedDomain,
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

    # Enforce foreign keys in tests so cascade/SET NULL behaviour and FK
    # violations are actually exercised in CI (SQLite defaults this OFF). This
    # mirrors the local-dev engine in app/db/base.py and is at least as strict
    # as production, where we now attempt the same PRAGMA on libsql connect.
    @event.listens_for(eng, "connect")
    def _fk_on(dbapi_conn, _):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

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
    """Seeds one UN-approved waitlist entry; returns the email. Being on the
    list no longer grants access — a super_admin must approve it."""
    from app.repositories import waitlist as waitlist_repo

    email = "founder@startup.com"
    waitlist_repo.add(db, email)
    db.commit()
    return email


@pytest.fixture
def approved_waitlist_email(db: Session) -> str:
    """Seeds an APPROVED waitlist entry (intended_tier='free'); returns the
    email. This is the default Allow path — auto-grants tier='free'."""
    from app.repositories import waitlist as waitlist_repo

    email = "approved@startup.com"
    entry = waitlist_repo.add(db, email)
    waitlist_repo.set_approved(db, entry, approved=True)
    db.commit()
    return email


@pytest.fixture
def approved_paid_waitlist_email(db: Session) -> str:
    """Seeds an APPROVED waitlist entry pre-marked intended_tier='paid';
    returns the email. Auto-grants tier='paid' on sign-in / claim."""
    from app.repositories import waitlist as waitlist_repo

    email = "vip@startup.com"
    entry = waitlist_repo.add(db, email)
    waitlist_repo.set_approved(db, entry, approved=True, intended_tier="paid")
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
