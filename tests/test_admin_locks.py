"""Admin lock-management endpoint tests (B5.3).

Covers super_admin endpoints for inspecting and clearing the 3-tier locks:
- GET /admin/locks/global (paginated, active 36h)
- GET /admin/locks/platform (permanent stops, unpaginated)
- DELETE /admin/locks/platform/{domain}
- DELETE /admin/locks/user/{user_id}/{domain}
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.deps import get_current_user
from app.db.session import get_db
from app.main import app
from app.models import User
from app.repositories import locks as locks_repo
from tests.conftest import _make_user


@pytest.fixture
def client_factory(engine: Engine):
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _make(user: User | None) -> TestClient:
        def _override_get_db():
            s = factory()
            try:
                yield s
            finally:
                s.close()

        def _override_get_current_user():
            if user is None:
                from fastapi import status

                from app.core.errors import ApiError

                raise ApiError(
                    "unauthorized",
                    "Not authenticated",
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )
            return user

        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[get_current_user] = _override_get_current_user
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


@pytest.fixture
def super_admin(db: Session) -> User:
    return _make_user(
        db,
        email="admin@knock.app",
        google_sub="g-admin",
        tier="super_admin",
        waitlist_email="admin@knock.app",
    )


@pytest.fixture
def regular_user(db: Session) -> User:
    return _make_user(
        db, email="u@x.com", tier="free", google_sub="g-u", waitlist_email="u@x.com"
    )


# ─────────────────────────── list ───────────────────────────


def test_admin_list_global_locks_paginated(
    client_factory, db: Session, super_admin: User, regular_user: User
) -> None:
    for domain in ("acme.com", "globex.com"):
        locks_repo.upsert_global_lock(db, domain, regular_user.id)
    db.commit()

    client = client_factory(super_admin)
    r = client.get("/api/v1/admin/locks/global")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    domains = {item["company_domain"] for item in body["items"]}
    assert domains == {"acme.com", "globex.com"}


def test_admin_list_platform_locks(
    client_factory, db: Session, super_admin: User
) -> None:
    locks_repo.upsert_platform_lock(db, "acme.com", reason="explicit_stop_reply")
    db.commit()

    client = client_factory(super_admin)
    r = client.get("/api/v1/admin/locks/platform")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["company_domain"] == "acme.com"
    assert rows[0]["reason"] == "explicit_stop_reply"


# ─────────────────────────── clear ───────────────────────────


def test_admin_clear_platform_lock(
    client_factory, db: Session, super_admin: User
) -> None:
    locks_repo.upsert_platform_lock(db, "acme.com", reason="explicit_stop_reply")
    db.commit()

    client = client_factory(super_admin)
    r = client.delete("/api/v1/admin/locks/platform/acme.com")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    db.expire_all()
    assert locks_repo.get_platform_lock(db, "acme.com") is None


def test_admin_clear_platform_lock_404(
    client_factory, super_admin: User
) -> None:
    client = client_factory(super_admin)
    r = client.delete("/api/v1/admin/locks/platform/nonexistent.com")
    assert r.status_code == 404


def test_admin_clear_user_company_lock(
    client_factory, db: Session, super_admin: User, regular_user: User
) -> None:
    locks_repo.upsert_user_company_lock(
        db, regular_user.id, "acme.com", reason="reply"
    )
    db.commit()

    client = client_factory(super_admin)
    r = client.delete(f"/api/v1/admin/locks/user/{regular_user.id}/acme.com")
    assert r.status_code == 200

    db.expire_all()
    assert locks_repo.get_user_company_lock(db, regular_user.id, "acme.com") is None


def test_admin_clear_user_lock_404(
    client_factory, super_admin: User, regular_user: User
) -> None:
    client = client_factory(super_admin)
    r = client.delete(f"/api/v1/admin/locks/user/{regular_user.id}/none.com")
    assert r.status_code == 404


# ─────────────────────────── gating ───────────────────────────


def test_non_super_admin_cant_list_locks(
    client_factory, regular_user: User
) -> None:
    client = client_factory(regular_user)
    r = client.get("/api/v1/admin/locks/global")
    assert r.status_code == 403


def test_non_super_admin_cant_clear_platform_lock(
    client_factory, db: Session, regular_user: User
) -> None:
    locks_repo.upsert_platform_lock(db, "acme.com", reason="manual_admin")
    db.commit()

    client = client_factory(regular_user)
    r = client.delete("/api/v1/admin/locks/platform/acme.com")
    assert r.status_code == 403


def test_unauthenticated_locks_endpoints(client_factory) -> None:
    client = client_factory(None)
    assert client.get("/api/v1/admin/locks/global").status_code == 401
    assert client.get("/api/v1/admin/locks/platform").status_code == 401
    assert client.delete("/api/v1/admin/locks/platform/acme.com").status_code == 401
