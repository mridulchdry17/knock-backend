"""Admin router tests — gating, tier mutations, pagination, CSV export.

Uses FastAPI TestClient against the live app, with `get_db` and
`get_current_user` overridden so tests focus on admin logic rather than the
session/bearer plumbing (which is covered by the Phase 2 auth tests).
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
from app.repositories import waitlist as waitlist_repo
from tests.conftest import _make_user


@pytest.fixture
def client_factory(engine: Engine):
    """Returns a callable that mounts a TestClient with the given user as
    `get_current_user`. Pass `user=None` to test unauthenticated.

    The DB session shared with the request handler is the same engine as
    the `db` fixture (via StaticPool), so mutations are visible across both.
    """
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


# ─────────────────────────── gating ───────────────────────────


def test_unauthenticated_returns_401(client_factory) -> None:
    client = client_factory(None)
    r = client.get("/api/v1/admin/users")
    assert r.status_code == 401


def test_free_user_gets_403(client_factory, db: Session) -> None:
    user = _make_user(
        db, email="user@example.com", tier="free", waitlist_email="user@example.com"
    )
    client = client_factory(user)
    r = client.get("/api/v1/admin/users")
    assert r.status_code == 403


def test_pending_user_gets_403(client_factory, db: Session) -> None:
    user = _make_user(db, email="pending@example.com", tier="pending")
    client = client_factory(user)
    r = client.get("/api/v1/admin/users")
    assert r.status_code == 403


def test_paid_user_gets_403(client_factory, db: Session) -> None:
    """Paid users are NOT super_admin — admin endpoints reject them."""
    user = _make_user(
        db, email="paid@example.com", tier="paid", waitlist_email="paid@example.com"
    )
    client = client_factory(user)
    r = client.get("/api/v1/admin/users")
    assert r.status_code == 403


def test_super_admin_gets_200(client_factory, super_admin: User) -> None:
    client = client_factory(super_admin)
    r = client.get("/api/v1/admin/users")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body


# ─────────────────────────── filtering ───────────────────────────


def test_filter_users_by_tier(client_factory, db: Session, super_admin: User) -> None:
    _make_user(db, email="p1@x.com", google_sub="g-p1", tier="pending")
    _make_user(db, email="p2@x.com", google_sub="g-p2", tier="pending")
    _make_user(db, email="f1@x.com", google_sub="g-f1", tier="free", waitlist_email="f1@x.com")

    client = client_factory(super_admin)
    r = client.get("/api/v1/admin/users?tier=pending")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert all(u["tier"] == "pending" for u in body["items"])


def test_search_users_by_email_substring(
    client_factory, db: Session, super_admin: User
) -> None:
    _make_user(
        db,
        email="alice@founders.com",
        google_sub="g-1",
        tier="free",
        waitlist_email="alice@founders.com",
    )
    _make_user(
        db,
        email="bob@elsewhere.com",
        google_sub="g-2",
        tier="free",
        waitlist_email="bob@elsewhere.com",
    )

    client = client_factory(super_admin)
    r = client.get("/api/v1/admin/users?search=founders")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["email"] == "alice@founders.com"


# ─────────────────────────── tier update ───────────────────────────


def test_promote_pending_to_free(client_factory, db: Session, super_admin: User) -> None:
    pending = _make_user(db, email="p@x.com", google_sub="g-p", tier="pending")
    client = client_factory(super_admin)

    r = client.patch(
        f"/api/v1/admin/users/{pending.id}/tier",
        json={"tier": "free"},
    )
    assert r.status_code == 200
    assert r.json()["tier"] == "free"

    db.refresh(pending)
    assert pending.tier == "free"
    assert pending.tier_set_at is not None


def test_invalid_tier_returns_422(client_factory, db: Session, super_admin: User) -> None:
    pending = _make_user(db, email="p@x.com", google_sub="g-p", tier="pending")
    client = client_factory(super_admin)

    r = client.patch(
        f"/api/v1/admin/users/{pending.id}/tier",
        json={"tier": "wizard"},
    )
    assert r.status_code == 422


def test_tier_update_unknown_user(client_factory, super_admin: User) -> None:
    client = client_factory(super_admin)
    r = client.patch(
        "/api/v1/admin/users/99999/tier",
        json={"tier": "free"},
    )
    assert r.status_code == 404


# ─────────────────────────── waitlist ───────────────────────────


def test_list_waitlist(client_factory, db: Session, super_admin: User) -> None:
    waitlist_repo.add(db, "first@example.com")
    waitlist_repo.add(db, "second@example.com")
    db.commit()

    client = client_factory(super_admin)
    # Default status=pending — both fresh rows are pending.
    r = client.get("/api/v1/admin/waitlist")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2


def test_list_waitlist_search_matches_email_substring(
    client_factory, db: Session, super_admin: User
) -> None:
    """Search filter is a case-insensitive substring match on email."""
    waitlist_repo.add(db, "alice@stripe.com")
    waitlist_repo.add(db, "bob@notion.so")
    waitlist_repo.add(db, "carol@stripe.com")
    db.commit()

    client = client_factory(super_admin)
    r = client.get("/api/v1/admin/waitlist?search=stripe")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    emails = sorted(item["email"] for item in body["items"])
    assert emails == ["alice@stripe.com", "carol@stripe.com"]

    # Case-insensitive.
    r2 = client.get("/api/v1/admin/waitlist?search=ALICE")
    assert r2.json()["total"] == 1


def test_list_waitlist_status_filter_pending_excludes_approved(
    client_factory, db: Session, super_admin: User
) -> None:
    e1 = waitlist_repo.add(db, "pending@x.com")
    e2 = waitlist_repo.add(db, "approved@x.com")
    waitlist_repo.set_approved(db, e2, approved=True)
    db.commit()

    client = client_factory(super_admin)
    r = client.get("/api/v1/admin/waitlist?status=pending")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == "pending@x.com"

    # And confirm e1 is still on the list (sanity).
    assert waitlist_repo.get(db, e1.id) is not None


def test_list_waitlist_status_filter_approved_excludes_pending(
    client_factory, db: Session, super_admin: User
) -> None:
    waitlist_repo.add(db, "pending@x.com")
    e2 = waitlist_repo.add(db, "approved@x.com")
    waitlist_repo.set_approved(db, e2, approved=True)
    db.commit()

    client = client_factory(super_admin)
    r = client.get("/api/v1/admin/waitlist?status=approved")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == "approved@x.com"


def test_list_waitlist_status_all_returns_both(
    client_factory, db: Session, super_admin: User
) -> None:
    waitlist_repo.add(db, "pending@x.com")
    e2 = waitlist_repo.add(db, "approved@x.com")
    waitlist_repo.set_approved(db, e2, approved=True)
    db.commit()

    client = client_factory(super_admin)
    r = client.get("/api/v1/admin/waitlist?status=all")
    assert r.json()["total"] == 2


def test_list_waitlist_sort_oldest(
    client_factory, db: Session, super_admin: User
) -> None:
    """Sort=oldest reverses the default newest-first order."""
    import time as _time

    waitlist_repo.add(db, "first@x.com")
    db.commit()
    _time.sleep(0.01)  # ensure created_at differs
    waitlist_repo.add(db, "second@x.com")
    db.commit()

    client = client_factory(super_admin)
    items = client.get("/api/v1/admin/waitlist?sort=oldest").json()["items"]
    assert [i["email"] for i in items[:2]] == ["first@x.com", "second@x.com"]

    items = client.get("/api/v1/admin/waitlist?sort=newest").json()["items"]
    assert [i["email"] for i in items[:2]] == ["second@x.com", "first@x.com"]


def test_list_waitlist_unknown_status_falls_back_to_all(
    client_factory, db: Session, super_admin: User
) -> None:
    """A typo in ?status= shouldn't 500 — fall back to all."""
    waitlist_repo.add(db, "x@y.com")
    db.commit()
    client = client_factory(super_admin)
    r = client.get("/api/v1/admin/waitlist?status=garbage")
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_waitlist_csv_export(client_factory, db: Session, super_admin: User) -> None:
    waitlist_repo.add(db, "x@y.com")
    waitlist_repo.add(db, "y@z.com")
    db.commit()

    client = client_factory(super_admin)
    r = client.get("/api/v1/admin/waitlist.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    body = r.text
    assert "id,email,created_at" in body
    assert "x@y.com" in body
    assert "y@z.com" in body


# ─────────────────────────── waitlist approve / revoke ───────────────────────────


def test_approve_waitlist_entry_stamps_approved_at(
    client_factory, db: Session, super_admin: User
) -> None:
    entry = waitlist_repo.add(db, "invitee@example.com")
    db.commit()
    assert entry.approved_at is None

    client = client_factory(super_admin)
    r = client.post(f"/api/v1/admin/waitlist/{entry.id}/approve")
    assert r.status_code == 200
    assert r.json()["approved_at"] is not None

    db.expire_all()
    assert waitlist_repo.get(db, entry.id).approved_at is not None


def test_approve_waitlist_promotes_linked_pending_user(
    client_factory, db: Session, super_admin: User
) -> None:
    """If the person already signed in and is sitting at 'pending', approving
    their waitlist entry bumps them to 'free' on the spot."""
    entry = waitlist_repo.add(db, "tester@example.com")
    db.commit()
    user = _make_user(
        db,
        email="tester@example.com",
        google_sub="g-tester",
        tier="pending",
        waitlist_email="tester@example.com",
    )

    client = client_factory(super_admin)
    r = client.post(f"/api/v1/admin/waitlist/{entry.id}/approve")
    assert r.status_code == 200

    db.expire_all()
    assert db.get(User, user.id).tier == "free"


def test_revoke_waitlist_entry_clears_and_downgrades_free_user(
    client_factory, db: Session, super_admin: User
) -> None:
    entry = waitlist_repo.add(db, "revoked@example.com")
    waitlist_repo.set_approved(db, entry, approved=True)
    db.commit()
    user = _make_user(
        db,
        email="revoked@example.com",
        google_sub="g-rev",
        tier="free",
        waitlist_email="revoked@example.com",
    )

    client = client_factory(super_admin)
    r = client.post(f"/api/v1/admin/waitlist/{entry.id}/revoke")
    assert r.status_code == 200
    assert r.json()["approved_at"] is None

    db.expire_all()
    assert waitlist_repo.get(db, entry.id).approved_at is None
    assert db.get(User, user.id).tier == "pending"


def test_approve_waitlist_as_paid_promotes_linked_pending_to_paid(
    client_factory, db: Session, super_admin: User
) -> None:
    """Pre-mark a waitlist entry as paid + link an already-pending user →
    that user jumps straight to 'paid', no need for a separate Promote click."""
    entry = waitlist_repo.add(db, "vip@example.com")
    db.commit()
    user = _make_user(
        db,
        email="vip@example.com",
        google_sub="g-vip",
        tier="pending",
        waitlist_email="vip@example.com",
    )

    client = client_factory(super_admin)
    r = client.post(
        f"/api/v1/admin/waitlist/{entry.id}/approve", json={"tier": "paid"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["approved_at"] is not None
    assert body["intended_tier"] == "paid"

    db.expire_all()
    assert db.get(User, user.id).tier == "paid"


def test_approve_waitlist_as_paid_bumps_already_free_user(
    client_factory, db: Session, super_admin: User
) -> None:
    """If a user was already 'free' (e.g. allowed earlier with default tier)
    and the admin re-allows the entry as paid, the linked user goes free→paid."""
    entry = waitlist_repo.add(db, "upgrade@example.com")
    waitlist_repo.set_approved(db, entry, approved=True)  # initially free
    db.commit()
    user = _make_user(
        db,
        email="upgrade@example.com",
        google_sub="g-up",
        tier="free",
        waitlist_email="upgrade@example.com",
    )

    client = client_factory(super_admin)
    r = client.post(
        f"/api/v1/admin/waitlist/{entry.id}/approve", json={"tier": "paid"}
    )
    assert r.status_code == 200

    db.expire_all()
    assert db.get(User, user.id).tier == "paid"


def test_approve_waitlist_empty_body_defaults_to_free(
    client_factory, db: Session, super_admin: User
) -> None:
    """Back-compat: existing callers send `{}` (or no body) and get the legacy
    Allow → free behaviour."""
    entry = waitlist_repo.add(db, "legacy@example.com")
    db.commit()

    client = client_factory(super_admin)
    r = client.post(f"/api/v1/admin/waitlist/{entry.id}/approve")
    assert r.status_code == 200
    assert r.json()["intended_tier"] == "free"


def test_revoke_resets_intended_tier_back_to_free(
    client_factory, db: Session, super_admin: User
) -> None:
    """A revoked entry shouldn't carry stale 'paid' state into a future
    re-approval — revoke resets intended_tier to 'free'."""
    entry = waitlist_repo.add(db, "rev@example.com")
    waitlist_repo.set_approved(db, entry, approved=True, intended_tier="paid")
    db.commit()

    client = client_factory(super_admin)
    r = client.post(f"/api/v1/admin/waitlist/{entry.id}/revoke")
    assert r.status_code == 200
    assert r.json()["intended_tier"] == "free"


def test_approve_waitlist_unknown_entry_404(client_factory, super_admin: User) -> None:
    client = client_factory(super_admin)
    assert client.post("/api/v1/admin/waitlist/99999/approve").status_code == 404


def test_approve_waitlist_requires_super_admin(client_factory, db: Session) -> None:
    user = _make_user(db, email="free@x.com", google_sub="g-f", tier="free")
    client = client_factory(user)
    assert client.post("/api/v1/admin/waitlist/1/approve").status_code == 403


# ─────────────────────────── bulk approve ───────────────────────────


def test_bulk_approve_happy_path(
    client_factory, db: Session, super_admin: User
) -> None:
    e1 = waitlist_repo.add(db, "a@x.com")
    e2 = waitlist_repo.add(db, "b@x.com")
    db.commit()

    client = client_factory(super_admin)
    r = client.post(
        "/api/v1/admin/waitlist/approve-bulk",
        json={"ids": [e1.id, e2.id]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["newly_approved"] == 2
    assert body["already_approved"] == 0
    assert body["not_found_ids"] == []

    db.expire_all()
    assert waitlist_repo.get(db, e1.id).approved_at is not None  # type: ignore[union-attr]
    assert waitlist_repo.get(db, e2.id).approved_at is not None  # type: ignore[union-attr]


def test_bulk_approve_mixed_pending_and_already_approved(
    client_factory, db: Session, super_admin: User
) -> None:
    """Mixed list: previously-approved rows count toward `already_approved`,
    not `newly_approved` — and they aren't double-stamped."""
    e1 = waitlist_repo.add(db, "pending@x.com")
    e2 = waitlist_repo.add(db, "already@x.com")
    waitlist_repo.set_approved(db, e2, approved=True)
    db.commit()
    original_approved_at = e2.approved_at

    client = client_factory(super_admin)
    r = client.post(
        "/api/v1/admin/waitlist/approve-bulk",
        json={"ids": [e1.id, e2.id]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["newly_approved"] == 1
    assert body["already_approved"] == 1
    assert body["not_found_ids"] == []

    db.expire_all()
    # The already-approved row's timestamp is preserved (no churn).
    e2_after = waitlist_repo.get(db, e2.id)
    assert e2_after is not None
    assert e2_after.approved_at == original_approved_at


def test_bulk_approve_returns_not_found_ids(
    client_factory, db: Session, super_admin: User
) -> None:
    e1 = waitlist_repo.add(db, "a@x.com")
    db.commit()

    client = client_factory(super_admin)
    r = client.post(
        "/api/v1/admin/waitlist/approve-bulk",
        json={"ids": [e1.id, 99999, 88888]},
    )
    body = r.json()
    assert body["newly_approved"] == 1
    assert sorted(body["not_found_ids"]) == [88888, 99999]


def test_bulk_approve_empty_ids_returns_zeros(
    client_factory, db: Session, super_admin: User
) -> None:
    client = client_factory(super_admin)
    r = client.post("/api/v1/admin/waitlist/approve-bulk", json={"ids": []})
    body = r.json()
    assert body == {"newly_approved": 0, "already_approved": 0, "not_found_ids": []}


def test_bulk_approve_promotes_linked_pending_users(
    client_factory, db: Session, super_admin: User
) -> None:
    """The per-row sync side-effect (promote pending user → free) must fire
    for each id in a bulk approval, identical to the single-row endpoint."""
    e = waitlist_repo.add(db, "tester@x.com")
    user = _make_user(
        db,
        email="tester@x.com",
        google_sub="g-tester",
        tier="pending",
        waitlist_email="tester@x.com",
    )
    db.commit()

    client = client_factory(super_admin)
    r = client.post(
        "/api/v1/admin/waitlist/approve-bulk",
        json={"ids": [e.id]},
    )
    assert r.status_code == 200
    db.expire_all()
    refreshed = db.get(type(user), user.id)
    assert refreshed is not None
    assert refreshed.tier == "free"


def test_bulk_approve_requires_super_admin(
    client_factory, db: Session
) -> None:
    user = _make_user(db, email="free@x.com", google_sub="g-f", tier="free")
    client = client_factory(user)
    r = client.post(
        "/api/v1/admin/waitlist/approve-bulk", json={"ids": [1]}
    )
    assert r.status_code == 403


# ─────────────────────────── pagination bounds ───────────────────────────


def test_invalid_pagination_returns_422(client_factory, super_admin: User) -> None:
    client = client_factory(super_admin)

    assert client.get("/api/v1/admin/users?limit=0").status_code == 422
    assert client.get("/api/v1/admin/users?limit=300").status_code == 422
    assert client.get("/api/v1/admin/users?offset=-1").status_code == 422


# ─────────────────────────── suspend/unsuspend ───────────────────────────


def test_suspend_and_unsuspend(client_factory, db: Session, super_admin: User) -> None:
    user = _make_user(
        db, email="u@x.com", google_sub="g-u", tier="free", waitlist_email="u@x.com"
    )
    client = client_factory(super_admin)

    r = client.post(f"/api/v1/admin/users/{user.id}/suspend")
    assert r.status_code == 200
    assert r.json()["is_suspended"] is True

    r = client.post(f"/api/v1/admin/users/{user.id}/unsuspend")
    assert r.status_code == 200
    assert r.json()["is_suspended"] is False
