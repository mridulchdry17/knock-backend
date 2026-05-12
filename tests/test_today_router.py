"""HTTP tests for /api/v1/today and the admin manual-trigger.

Reuses the client_factory pattern from test_admin.py — overrides
get_current_user + get_db on the shared TestClient.
"""
from __future__ import annotations

from random import Random

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.deps import get_current_user
from app.core.time import utcnow
from app.db.session import get_db
from app.main import app
from app.models import Company, Contact, User
from app.services import batch_generator as batch_gen_svc
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
                    status_code=401 if False else status.HTTP_401_UNAUTHORIZED,
                )
            return user

        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[get_current_user] = _override_get_current_user
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


@pytest.fixture
def free_user(db: Session) -> User:
    user = _make_user(
        db,
        email="free@x.com",
        google_sub="g-free",
        tier="free",
        waitlist_email="free@x.com",
    )
    user.google_refresh_token = "fake"
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def super_admin(db: Session) -> User:
    user = _make_user(
        db,
        email="admin@knock.app",
        google_sub="g-admin",
        tier="super_admin",
        waitlist_email="admin@knock.app",
    )
    user.google_refresh_token = "fake"
    db.add(user)
    db.commit()
    return user


def _seed_pool(db: Session, *, n: int) -> list[Company]:
    out: list[Company] = []
    for i in range(n):
        co = Company(domain=f"c{i}.com", name=f"Co {i}", source="seed")
        db.add(co)
        db.flush()
        for j in range(2):
            db.add(
                Contact(
                    company_id=co.id,
                    email=f"u{i}_{j}@c{i}.com",
                    name=f"User {i}{j}",
                )
            )
        out.append(co)
    db.commit()
    return out


# ─────────────────────────── gating ───────────────────────────


def test_unauthenticated_returns_401(client_factory) -> None:
    client = client_factory(None)
    r = client.get("/api/v1/today")
    assert r.status_code == 401


def test_pending_user_returns_403(client_factory, db: Session) -> None:
    user = _make_user(db, email="p@x.com", google_sub="g-p", tier="pending")
    client = client_factory(user)
    r = client.get("/api/v1/today")
    assert r.status_code == 403


# ─────────────────────────── GET /today ───────────────────────────


def test_get_today_returns_404_when_no_batch(client_factory, free_user: User) -> None:
    client = client_factory(free_user)
    r = client.get("/api/v1/today")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "no_batch_yet"


def test_get_today_returns_batch_shape(
    client_factory, db: Session, free_user: User
) -> None:
    _seed_pool(db, n=3)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    client = client_factory(free_user)
    r = client.get("/api/v1/today")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {
        "generated_at",
        "date",
        "cap",
        "sent_today",
        "items",
    }
    assert len(body["items"]) == 3
    item = body["items"][0]
    assert set(item.keys()) >= {
        "id",
        "recipient",
        "cc_recipients",
        "subject",
        "body_preview",
        "body",
        "send_time",
        "status",
    }
    # Recipient shape matches the F.5a contract.
    assert set(item["recipient"].keys()) >= {
        "name",
        "email",
        "role",
        "company",
        "company_domain",
    }


# ─────────────────────────── PATCH /today/items/{id} ───────────────────────────


def test_patch_subject_auto_marks_ready(
    client_factory, db: Session, free_user: User
) -> None:
    _seed_pool(db, n=2)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    client = client_factory(free_user)

    body = client.get("/api/v1/today").json()
    item_id = body["items"][0]["id"]

    r = client.patch(
        f"/api/v1/today/items/{item_id}",
        json={"subject": "New subject please"},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["subject"] == "New subject please"
    assert out["status"] == "ready"


def test_patch_status_skipped_works(
    client_factory, db: Session, free_user: User
) -> None:
    _seed_pool(db, n=2)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    client = client_factory(free_user)
    item_id = client.get("/api/v1/today").json()["items"][0]["id"]

    r = client.patch(
        f"/api/v1/today/items/{item_id}",
        json={"status": "skipped"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"


def test_patch_item_belonging_to_another_user_returns_404(
    client_factory, db: Session, free_user: User
) -> None:
    _seed_pool(db, n=2)
    other = _make_user(
        db,
        email="other@x.com",
        google_sub="g-other",
        tier="free",
        waitlist_email="other@x.com",
    )
    other.google_refresh_token = "fake"
    db.add(other)
    db.commit()
    batch_gen_svc.generate_batch_for_user(
        db, other, batch_date=utcnow().date(), rng=Random(1)
    )

    client = client_factory(free_user)
    # Find one of `other`'s items via direct lookup (not via free_user's /today).
    from app.repositories import today_batch as today_repo

    others_items = today_repo.list_for_user_date(db, other.id, utcnow().date())
    target = others_items[0].id

    r = client.patch(f"/api/v1/today/items/{target}", json={"subject": "x"})
    assert r.status_code == 404


def test_patch_explicit_status_wins_over_edit_auto_ready(
    client_factory, db: Session, free_user: User
) -> None:
    _seed_pool(db, n=1)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    client = client_factory(free_user)
    item_id = client.get("/api/v1/today").json()["items"][0]["id"]

    r = client.patch(
        f"/api/v1/today/items/{item_id}",
        json={"subject": "edited", "status": "default"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "default"


# ─────────────────────────── admin manual trigger ───────────────────────────


def test_admin_trigger_requires_super_admin(
    client_factory, free_user: User
) -> None:
    client = client_factory(free_user)
    r = client.post("/api/v1/admin/today/run-cron")
    assert r.status_code == 403


def test_admin_trigger_runs_for_all_users(
    client_factory, db: Session, super_admin: User
) -> None:
    _make_user(
        db, email="f@x.com", google_sub="g-f", tier="free", waitlist_email="f@x.com"
    )
    # Re-fetch so we can set the token.
    from app.repositories import users as users_repo

    f = users_repo.get_by_email(db, "f@x.com")
    assert f is not None
    f.google_refresh_token = "fake"
    db.add(f)
    db.commit()
    _seed_pool(db, n=3)

    client = client_factory(super_admin)
    r = client.post("/api/v1/admin/today/run-cron")
    assert r.status_code == 200
    body = r.json()
    assert body["total_users_processed"] >= 2  # super_admin + free
    # super_admin is gmail-connected via the fixture; should produce items.
    assert body["total_items_created"] >= 3


def test_admin_trigger_with_target_user_id(
    client_factory, db: Session, super_admin: User
) -> None:
    _seed_pool(db, n=3)
    client = client_factory(super_admin)
    r = client.post(
        f"/api/v1/admin/today/run-cron?target_user_id={super_admin.id}"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_users_processed"] == 1
    assert body["results"][0]["user_id"] == super_admin.id


def test_admin_trigger_target_user_not_found(
    client_factory, super_admin: User
) -> None:
    client = client_factory(super_admin)
    r = client.post("/api/v1/admin/today/run-cron?target_user_id=99999")
    assert r.status_code == 404
