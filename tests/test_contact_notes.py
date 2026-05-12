"""Tests for B5.1b per-user contact notes (`/api/v1/contacts/{id}/my-notes`).

Surfaces under test:
- GET: returns existing row, 404 on missing row OR missing contact
- PUT: idempotent upsert; empty string deletes (204); updates advance updated_at
- DELETE: 200 on success, 404 if no note exists
- Isolation: User A's note is invisible to User B
- Validation: 5000-char ceiling enforced via 422
- AuthN: bearer required (401)
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.deps import get_current_user
from app.db.session import get_db
from app.main import app
from app.models import Company, Contact, User
from app.repositories import user_contact_notes as notes_repo
from tests.conftest import _make_user

# ─────────────────────────── fixtures ───────────────────────────


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
def user_a(db: Session) -> User:
    return _make_user(
        db, email="a@x.com", google_sub="g-a", tier="free", waitlist_email="a@x.com"
    )


@pytest.fixture
def user_b(db: Session) -> User:
    return _make_user(
        db, email="b@x.com", google_sub="g-b", tier="free", waitlist_email="b@x.com"
    )


@pytest.fixture
def contact(db: Session) -> Contact:
    company = Company(name="Acme", domain="acme.com", source="manual")
    db.add(company)
    db.flush()
    c = Contact(company_id=company.id, name="Sarah", email="sarah@acme.com")
    db.add(c)
    db.commit()
    return c


# ─────────────────────────── GET ───────────────────────────


def test_get_my_note_404_when_missing(
    client_factory, user_a: User, contact: Contact
) -> None:
    client = client_factory(user_a)
    r = client.get(f"/api/v1/contacts/{contact.id}/my-notes")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_get_my_note_returns_existing(
    client_factory, db: Session, user_a: User, contact: Contact
) -> None:
    client = client_factory(user_a)
    notes_repo.upsert(db, user_a.id, contact.id, "She retweets papers from advisor's lab.")
    db.commit()

    r = client.get(f"/api/v1/contacts/{contact.id}/my-notes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["contact_id"] == contact.id
    assert body["notes"] == "She retweets papers from advisor's lab."
    assert "created_at" in body
    assert "updated_at" in body


def test_my_note_unknown_contact_404(client_factory, user_a: User) -> None:
    client = client_factory(user_a)
    r = client.get("/api/v1/contacts/99999/my-notes")
    assert r.status_code == 404


# ─────────────────────────── PUT ───────────────────────────


def test_upsert_my_note_inserts(
    client_factory, db: Session, user_a: User, contact: Contact
) -> None:
    client = client_factory(user_a)
    r = client.put(
        f"/api/v1/contacts/{contact.id}/my-notes",
        json={"notes": "First note."},
    )
    assert r.status_code == 200, r.text
    assert r.json()["notes"] == "First note."

    db.expire_all()
    row = notes_repo.get(db, user_a.id, contact.id)
    assert row is not None
    assert row.notes == "First note."


def test_upsert_my_note_updates(
    client_factory, db: Session, user_a: User, contact: Contact
) -> None:
    client = client_factory(user_a)
    r = client.put(
        f"/api/v1/contacts/{contact.id}/my-notes", json={"notes": "first"}
    )
    assert r.status_code == 200
    first_updated_at = r.json()["updated_at"]

    # Force a measurable wall-clock gap so updated_at advances on the second
    # call even on fast machines.
    time.sleep(0.01)

    r2 = client.put(
        f"/api/v1/contacts/{contact.id}/my-notes", json={"notes": "second"}
    )
    assert r2.status_code == 200
    assert r2.json()["notes"] == "second"
    assert r2.json()["updated_at"] >= first_updated_at


def test_upsert_my_note_empty_string_deletes(
    client_factory, db: Session, user_a: User, contact: Contact
) -> None:
    client = client_factory(user_a)
    client.put(f"/api/v1/contacts/{contact.id}/my-notes", json={"notes": "to be cleared"})

    r = client.put(
        f"/api/v1/contacts/{contact.id}/my-notes", json={"notes": ""}
    )
    assert r.status_code == 204
    assert r.content == b""

    db.expire_all()
    assert notes_repo.get(db, user_a.id, contact.id) is None


def test_upsert_my_note_whitespace_only_deletes(
    client_factory, db: Session, user_a: User, contact: Contact
) -> None:
    """`   ` after .strip() is empty — should delete, not insert blank junk."""
    client = client_factory(user_a)
    client.put(f"/api/v1/contacts/{contact.id}/my-notes", json={"notes": "real"})

    r = client.put(
        f"/api/v1/contacts/{contact.id}/my-notes", json={"notes": "   \n  "}
    )
    assert r.status_code == 204

    db.expire_all()
    assert notes_repo.get(db, user_a.id, contact.id) is None


def test_my_note_max_length_5000(
    client_factory, user_a: User, contact: Contact
) -> None:
    client = client_factory(user_a)
    r = client.put(
        f"/api/v1/contacts/{contact.id}/my-notes",
        json={"notes": "x" * 5001},
    )
    assert r.status_code == 422


def test_my_note_max_length_5000_exact(
    client_factory, user_a: User, contact: Contact
) -> None:
    client = client_factory(user_a)
    r = client.put(
        f"/api/v1/contacts/{contact.id}/my-notes",
        json={"notes": "x" * 5000},
    )
    assert r.status_code == 200


# ─────────────────────────── DELETE ───────────────────────────


def test_delete_my_note_success(
    client_factory, db: Session, user_a: User, contact: Contact
) -> None:
    client = client_factory(user_a)
    notes_repo.upsert(db, user_a.id, contact.id, "soon to be gone")
    db.commit()

    r = client.delete(f"/api/v1/contacts/{contact.id}/my-notes")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    db.expire_all()
    assert notes_repo.get(db, user_a.id, contact.id) is None


def test_delete_my_note_404_when_missing(
    client_factory, user_a: User, contact: Contact
) -> None:
    client = client_factory(user_a)
    r = client.delete(f"/api/v1/contacts/{contact.id}/my-notes")
    assert r.status_code == 404


# ─────────────────────────── isolation + auth ───────────────────────────


def test_my_note_isolated_per_user(
    client_factory, db: Session, user_a: User, user_b: User, contact: Contact
) -> None:
    """User A's note must not appear when User B reads."""
    client_a = client_factory(user_a)
    client_a.put(
        f"/api/v1/contacts/{contact.id}/my-notes",
        json={"notes": "A's private take."},
    )

    client_b = client_factory(user_b)
    r = client_b.get(f"/api/v1/contacts/{contact.id}/my-notes")
    assert r.status_code == 404

    # B writes their own — A's is untouched
    client_b.put(
        f"/api/v1/contacts/{contact.id}/my-notes",
        json={"notes": "B's private take."},
    )

    db.expire_all()
    row_a = notes_repo.get(db, user_a.id, contact.id)
    row_b = notes_repo.get(db, user_b.id, contact.id)
    assert row_a is not None and row_a.notes == "A's private take."
    assert row_b is not None and row_b.notes == "B's private take."


def test_my_note_requires_auth(client_factory, contact: Contact) -> None:
    client = client_factory(None)
    r = client.get(f"/api/v1/contacts/{contact.id}/my-notes")
    assert r.status_code == 401


def test_my_note_pending_user_forbidden(
    client_factory, db: Session, contact: Contact
) -> None:
    """Pending users (awaiting approval) should not access the notes API."""
    pending = _make_user(
        db, email="p@x.com", google_sub="g-p", tier="pending", waitlist_email=None
    )
    client = client_factory(pending)
    r = client.get(f"/api/v1/contacts/{contact.id}/my-notes")
    assert r.status_code == 403
