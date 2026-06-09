"""Tests for B5.1 admin contact upload.

Covers:
- Bulk JSON + CSV upload happy path against the real-world fixture
- Dedup on second upload
- Per-row validation errors (missing/invalid email)
- Column aliasing (Name/Email/Title/Company → name/email/role/company_name)
- String hygiene (trailing punctuation, whitespace)
- Derivation (company_domain from email, company_name from domain)
- dry_run mode
- Gating (non-super_admin gets 403)
- Listing, search, deletion, company summary
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.deps import get_current_user
from app.db.session import get_db
from app.main import app
from app.models import Company, Contact, User
from app.repositories import contacts as contacts_repo
from tests.conftest import _make_user

FIXTURE = Path(__file__).parent / "fixtures" / "contacts_sample.csv"


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
def super_admin(db: Session) -> User:
    return _make_user(
        db,
        email="admin@knock.app",
        google_sub="g-admin",
        tier="super_admin",
        waitlist_email="admin@knock.app",
    )


def _upload_csv(client: TestClient, *, dry_run: bool = False) -> dict:
    with FIXTURE.open("rb") as f:
        files = {"file": ("contacts_sample.csv", f, "text/csv")}
        r = client.post(
            f"/api/v1/admin/contacts/bulk/csv?dry_run={'true' if dry_run else 'false'}",
            files=files,
        )
    assert r.status_code == 200, r.text
    return r.json()


# ─────────────────────────── happy paths ───────────────────────────


def test_upload_csv_happy_path(client_factory, db: Session, super_admin: User) -> None:
    client = client_factory(super_admin)
    body = _upload_csv(client)

    assert body["inserted"] == 12
    assert body["updated"] == 0
    assert body["failed"] == 0
    assert body["row_errors"] == []

    # Spot-check that derived company_domain matches email domain
    contact = contacts_repo.get_by_email(db, "akhila@estuate.com")
    assert contact is not None
    company = db.get(Company, contact.company_id)
    assert company is not None
    assert company.domain == "estuate.com"


def test_upload_preserves_notes_and_source(
    client_factory, db: Session, super_admin: User
) -> None:
    """B5.1b: the fixture's notes/source columns round-trip onto Contact rows."""
    client = client_factory(super_admin)
    _upload_csv(client)

    # Row with non-empty Notes
    c = contacts_repo.get_by_email(db, "akanksha.puri@sourcefuse.com")
    assert c is not None
    assert c.notes == "Hires interns each spring; former IIT-D."
    assert c.source == "manual-2026-dump"

    # Row with empty Notes (whitespace-only) → None, source still set
    c2 = contacts_repo.get_by_email(db, "akanksha.sogani@perennialsys.com")
    assert c2 is not None
    assert c2.notes is None
    assert c2.source == "manual-2026-dump"


def test_upload_doesnt_clobber_existing_notes(
    client_factory, db: Session, super_admin: User
) -> None:
    """Re-upload a row with empty notes — original notes are preserved."""
    client = client_factory(super_admin)
    # Initial upload sets notes
    r = client.post(
        "/api/v1/admin/contacts/bulk",
        json={
            "rows": [
                {
                    "email": "alice@acme.com",
                    "name": "Alice",
                    "notes": "Met at hackathon.",
                    "source": "referral-aman",
                }
            ]
        },
    )
    assert r.status_code == 200

    # Re-upload same email, no notes/source
    r = client.post(
        "/api/v1/admin/contacts/bulk",
        json={"rows": [{"email": "alice@acme.com", "name": "Alice Chen"}]},
    )
    assert r.status_code == 200
    assert r.json()["updated"] == 1

    db.expire_all()
    c = contacts_repo.get_by_email(db, "alice@acme.com")
    assert c is not None
    assert c.name == "Alice Chen"
    assert c.notes == "Met at hackathon."  # preserved
    assert c.source == "referral-aman"  # preserved


def test_admin_contact_out_includes_notes_and_source(
    client_factory, db: Session, super_admin: User
) -> None:
    client = client_factory(super_admin)
    _upload_csv(client)

    r = client.get("/api/v1/admin/contacts?search=akanksha.puri")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["notes"] == "Hires interns each spring; former IIT-D."
    assert items[0]["source"] == "manual-2026-dump"


def test_upload_dedup(client_factory, db: Session, super_admin: User) -> None:
    client = client_factory(super_admin)
    first = _upload_csv(client)
    assert first["inserted"] == 12

    second = _upload_csv(client)
    assert second["inserted"] == 0
    assert second["updated"] == 12
    assert second["failed"] == 0


def test_upload_trailing_punctuation_stripped(
    client_factory, db: Session, super_admin: User
) -> None:
    """Row 4 of the fixture has Company='Estuate,' — verify the comma is stripped."""
    client = client_factory(super_admin)
    _upload_csv(client)

    contact = contacts_repo.get_by_email(db, "akhila@estuate.com")
    assert contact is not None
    company = db.get(Company, contact.company_id)
    assert company is not None
    assert company.name == "Estuate"  # not 'Estuate,'


def test_upload_company_domain_derived_from_email(
    client_factory, db: Session, super_admin: User
) -> None:
    client = client_factory(super_admin)
    r = client.post(
        "/api/v1/admin/contacts/bulk",
        json={"rows": [{"email": "Jane@Example.IO", "name": "Jane"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 1

    contact = contacts_repo.get_by_email(db, "jane@example.io")
    assert contact is not None
    company = db.get(Company, contact.company_id)
    assert company is not None
    assert company.domain == "example.io"


def test_upload_company_name_derived_from_domain(
    client_factory, db: Session, super_admin: User
) -> None:
    client = client_factory(super_admin)
    r = client.post(
        "/api/v1/admin/contacts/bulk",
        json={"rows": [{"email": "x@acme.com"}]},
    )
    assert r.status_code == 200, r.text
    contact = contacts_repo.get_by_email(db, "x@acme.com")
    assert contact is not None
    company = db.get(Company, contact.company_id)
    assert company is not None
    assert company.name == "Acme"


def test_upload_case_insensitive_columns(
    client_factory, db: Session, super_admin: User
) -> None:
    client = client_factory(super_admin)
    r = client.post(
        "/api/v1/admin/contacts/bulk",
        json={
            "rows": [
                {
                    "Name": "Alice",
                    "EMAIL": "alice@startup.com",
                    "Title": "Head of HR",
                    "Company": "Startup",
                }
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 1

    contact = contacts_repo.get_by_email(db, "alice@startup.com")
    assert contact is not None
    assert contact.name == "Alice"
    assert contact.role == "Head of HR"
    company = db.get(Company, contact.company_id)
    assert company is not None
    assert company.name == "Startup"


# ─────────────────────────── validation errors ───────────────────────────


def test_upload_missing_email_rejected(
    client_factory, db: Session, super_admin: User
) -> None:
    client = client_factory(super_admin)
    r = client.post(
        "/api/v1/admin/contacts/bulk",
        json={"rows": [{"name": "No Email", "email": ""}, {"email": "ok@x.com"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] == 1
    assert body["failed"] == 1
    assert body["row_errors"][0]["error_code"] == "missing_email"
    assert body["row_errors"][0]["row_index"] == 0


def test_upload_invalid_email_rejected(
    client_factory, db: Session, super_admin: User
) -> None:
    client = client_factory(super_admin)
    r = client.post(
        "/api/v1/admin/contacts/bulk",
        json={"rows": [{"email": "not-an-email"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["failed"] == 1
    assert body["row_errors"][0]["error_code"] == "invalid_email"


def test_dry_run_doesnt_persist(client_factory, db: Session, super_admin: User) -> None:
    client = client_factory(super_admin)
    body = _upload_csv(client, dry_run=True)
    assert body["inserted"] == 12

    assert db.query(Contact).count() == 0
    assert db.query(Company).count() == 0


# ─────────────────────────── gating ───────────────────────────


def test_non_super_admin_gets_403(client_factory, db: Session) -> None:
    for tier in ("free", "paid", "pending"):
        user = _make_user(
            db,
            email=f"{tier}@x.com",
            google_sub=f"g-{tier}",
            tier=tier,
            waitlist_email=f"{tier}@x.com" if tier != "pending" else None,
        )
        client = client_factory(user)
        r = client.post("/api/v1/admin/contacts/bulk", json={"rows": []})
        assert r.status_code == 403, f"{tier} should be forbidden"


# ─────────────────────────── listing ───────────────────────────


def test_list_contacts_pagination(
    client_factory, db: Session, super_admin: User
) -> None:
    client = client_factory(super_admin)
    rows = [{"email": f"user{i}@example.com", "name": f"User {i}"} for i in range(60)]
    r = client.post("/api/v1/admin/contacts/bulk", json={"rows": rows})
    assert r.status_code == 200
    assert r.json()["inserted"] == 60

    r = client.get("/api/v1/admin/contacts?limit=25&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 60
    assert len(body["items"]) == 25

    r = client.get("/api/v1/admin/contacts?limit=25&offset=50")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 10


def test_list_contacts_search(client_factory, db: Session, super_admin: User) -> None:
    client = client_factory(super_admin)
    client.post(
        "/api/v1/admin/contacts/bulk",
        json={
            "rows": [
                {"email": "alice@acme.com", "name": "Alice"},
                {"email": "bob@elsewhere.com", "name": "Bob"},
            ]
        },
    )

    r = client.get("/api/v1/admin/contacts?search=acme")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["email"] == "alice@acme.com"


def test_list_contacts_filter_by_company_domain(
    client_factory, db: Session, super_admin: User
) -> None:
    client = client_factory(super_admin)
    client.post(
        "/api/v1/admin/contacts/bulk",
        json={
            "rows": [
                {"email": "a1@acme.com"},
                {"email": "a2@acme.com"},
                {"email": "b1@other.com"},
            ]
        },
    )

    r = client.get("/api/v1/admin/contacts?company_domain=acme.com")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2


def test_companies_summary(client_factory, db: Session, super_admin: User) -> None:
    client = client_factory(super_admin)
    _upload_csv(client)

    r = client.get("/api/v1/admin/contacts/companies/summary")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 12  # 12 distinct companies in the fixture
    for entry in body:
        assert entry["contact_count"] == 1
        assert "company_domain" in entry
        assert "company_name" in entry


def test_delete_contact(client_factory, db: Session, super_admin: User) -> None:
    client = client_factory(super_admin)
    client.post(
        "/api/v1/admin/contacts/bulk",
        json={"rows": [{"email": "doomed@x.com", "name": "Doomed"}]},
    )
    contact = contacts_repo.get_by_email(db, "doomed@x.com")
    assert contact is not None

    r = client.delete(f"/api/v1/admin/contacts/{contact.id}")
    assert r.status_code == 200

    db.expire_all()
    assert contacts_repo.get_by_email(db, "doomed@x.com") is None


def test_delete_contact_not_found(client_factory, super_admin: User) -> None:
    client = client_factory(super_admin)
    r = client.delete("/api/v1/admin/contacts/99999")
    assert r.status_code == 404
