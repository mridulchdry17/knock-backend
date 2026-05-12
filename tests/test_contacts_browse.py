"""Router tests for the read-only contact browse (B5.3).

Covers:
- /api/v1/contacts (list) — filtering by exclusions + locks, availability surfacing
- /api/v1/contacts/{id} (detail) — joined notes + lock state
- Tier gating (pending → 403)
- Auth requirements (401)
- Per-user isolation of reply locks
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.deps import get_current_user
from app.db.session import get_db
from app.main import app
from app.models import Company, Contact, User
from app.repositories import locks as locks_repo
from app.repositories import preferences as prefs_repo
from app.repositories import user_contact_notes as notes_repo
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
def user_a(db: Session) -> User:
    return _make_user(
        db, email="a@x.com", google_sub="g-a", tier="free", waitlist_email="a@x.com"
    )


@pytest.fixture
def user_b(db: Session) -> User:
    return _make_user(
        db, email="b@x.com", google_sub="g-b", tier="free", waitlist_email="b@x.com"
    )


def _make_company(db: Session, name: str, domain: str) -> Company:
    co = Company(name=name, domain=domain, source="manual")
    db.add(co)
    db.flush()
    return co


def _make_contact(
    db: Session, company: Company, email: str, name: str | None = None
) -> Contact:
    c = Contact(
        company_id=company.id,
        name=name,
        email=email,
        notes=None,
    )
    db.add(c)
    db.flush()
    return c


@pytest.fixture
def seeded(db: Session) -> dict:
    acme = _make_company(db, "Acme", "acme.com")
    globex = _make_company(db, "Globex", "globex.com")
    initech = _make_company(db, "Initech", "initech.com")
    c1 = _make_contact(db, acme, "sarah@acme.com", "Sarah")
    c2 = _make_contact(db, globex, "ryan@globex.com", "Ryan")
    c3 = _make_contact(db, initech, "mia@initech.com", "Mia")
    db.commit()
    return {
        "acme": acme,
        "globex": globex,
        "initech": initech,
        "c1": c1,
        "c2": c2,
        "c3": c3,
    }


# ─────────────────────────── browse list ───────────────────────────


def test_browse_lists_all_when_no_filters(
    client_factory, user_a: User, seeded: dict
) -> None:
    client = client_factory(user_a)
    r = client.get("/api/v1/contacts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    emails = {item["email"] for item in body["items"]}
    assert emails == {"sarah@acme.com", "ryan@globex.com", "mia@initech.com"}


def test_browse_excludes_excluded_domains(
    client_factory, db: Session, user_a: User, seeded: dict
) -> None:
    prefs_repo.add_excluded_domain(db, user_a.id, "globex.com")
    db.commit()

    client = client_factory(user_a)
    r = client.get("/api/v1/contacts")
    assert r.status_code == 200
    emails = {item["email"] for item in r.json()["items"]}
    assert "ryan@globex.com" not in emails
    assert emails == {"sarah@acme.com", "mia@initech.com"}


def test_browse_excludes_platform_permanent_locks(
    client_factory, db: Session, user_a: User, user_b: User, seeded: dict
) -> None:
    locks_repo.upsert_platform_lock(db, "acme.com", reason="explicit_stop_reply")
    db.commit()

    for u in (user_a, user_b):
        client = client_factory(u)
        r = client.get("/api/v1/contacts")
        emails = {item["email"] for item in r.json()["items"]}
        assert "sarah@acme.com" not in emails


def test_browse_excludes_per_user_reply_locks(
    client_factory, db: Session, user_a: User, user_b: User, seeded: dict
) -> None:
    locks_repo.upsert_user_company_lock(
        db, user_a.id, "acme.com", reason="reply"
    )
    db.commit()

    r_a = client_factory(user_a).get("/api/v1/contacts")
    r_b = client_factory(user_b).get("/api/v1/contacts")
    emails_a = {item["email"] for item in r_a.json()["items"]}
    emails_b = {item["email"] for item in r_b.json()["items"]}

    assert "sarah@acme.com" not in emails_a
    assert "sarah@acme.com" in emails_b  # B is unaffected


def test_browse_includes_36h_cooldown_with_status(
    client_factory, db: Session, user_a: User, seeded: dict
) -> None:
    """36h cooldown is surfaced as availability status, not hidden from browse."""
    locks_repo.upsert_global_lock(db, "acme.com", user_a.id)
    db.commit()

    client = client_factory(user_a)
    r = client.get("/api/v1/contacts")
    assert r.status_code == 200
    items = r.json()["items"]
    acme_item = next(item for item in items if item["email"] == "sarah@acme.com")
    assert acme_item["availability"]["status"] == "platform_cooldown"
    assert acme_item["availability"]["available_at"] is not None


def test_browse_filter_by_company_domain(
    client_factory, user_a: User, seeded: dict
) -> None:
    client = client_factory(user_a)
    r = client.get("/api/v1/contacts", params={"company_domain": "acme.com"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert {item["email"] for item in items} == {"sarah@acme.com"}


def test_browse_search_by_name(client_factory, user_a: User, seeded: dict) -> None:
    client = client_factory(user_a)
    r = client.get("/api/v1/contacts", params={"search": "sarah"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert {item["email"] for item in items} == {"sarah@acme.com"}


def test_browse_excludes_invalid_contacts(
    client_factory, db: Session, user_a: User, seeded: dict
) -> None:
    seeded["c1"].is_invalid = True
    db.add(seeded["c1"])
    db.commit()

    client = client_factory(user_a)
    r = client.get("/api/v1/contacts")
    emails = {item["email"] for item in r.json()["items"]}
    assert "sarah@acme.com" not in emails


def test_browse_pagination(
    client_factory, db: Session, user_a: User, seeded: dict
) -> None:
    client = client_factory(user_a)
    r = client.get("/api/v1/contacts", params={"limit": 2, "offset": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


def test_pagination_bounds_422(client_factory, user_a: User) -> None:
    client = client_factory(user_a)
    r = client.get("/api/v1/contacts", params={"limit": 0})
    assert r.status_code == 422
    r = client.get("/api/v1/contacts", params={"limit": 500})
    assert r.status_code == 422


def test_browse_requires_auth(client_factory) -> None:
    client = client_factory(None)
    r = client.get("/api/v1/contacts")
    assert r.status_code == 401


def test_browse_pending_tier_403(client_factory, db: Session) -> None:
    pending = _make_user(db, email="p@x.com", tier="pending", google_sub="g-p")
    client = client_factory(pending)
    r = client.get("/api/v1/contacts")
    assert r.status_code == 403


# ─────────────────────────── detail ───────────────────────────


def test_detail_hydrates_notes_and_locks(
    client_factory, db: Session, user_a: User, seeded: dict
) -> None:
    contact = seeded["c1"]
    contact.notes = "Shared admin note: prefers warm intros."
    contact.linkedin_url = "https://linkedin.com/in/sarah"
    db.add(contact)
    notes_repo.upsert(db, user_a.id, contact.id, "She retweets advisor's lab.")
    locks_repo.upsert_global_lock(db, "acme.com", user_a.id)
    db.commit()

    client = client_factory(user_a)
    r = client.get(f"/api/v1/contacts/{contact.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "sarah@acme.com"
    assert body["company_domain"] == "acme.com"
    assert body["shared_notes"] == "Shared admin note: prefers warm intros."
    assert body["my_notes"] == "She retweets advisor's lab."
    assert body["linkedin_url"] == "https://linkedin.com/in/sarah"
    assert body["availability"]["status"] == "platform_cooldown"


def test_detail_shows_platform_permanent_lock(
    client_factory, db: Session, user_a: User, seeded: dict
) -> None:
    """Detail intentionally surfaces locked contacts (unlike browse)."""
    locks_repo.upsert_platform_lock(db, "acme.com", reason="explicit_stop_reply")
    db.commit()

    client = client_factory(user_a)
    r = client.get(f"/api/v1/contacts/{seeded['c1'].id}")
    assert r.status_code == 200
    assert r.json()["availability"]["status"] == "platform_permanent"
    assert r.json()["availability"]["available_at"] is None


def test_detail_unknown_contact_404(client_factory, user_a: User) -> None:
    client = client_factory(user_a)
    r = client.get("/api/v1/contacts/99999")
    assert r.status_code == 404


def test_detail_my_notes_null_when_absent(
    client_factory, user_a: User, seeded: dict
) -> None:
    client = client_factory(user_a)
    r = client.get(f"/api/v1/contacts/{seeded['c1'].id}")
    assert r.status_code == 200
    assert r.json()["my_notes"] is None


def test_detail_requires_auth(client_factory, seeded: dict) -> None:
    client = client_factory(None)
    r = client.get(f"/api/v1/contacts/{seeded['c1'].id}")
    assert r.status_code == 401


def test_browse_user_lock_expired_naturally_reappears(
    client_factory, db: Session, user_a: User, seeded: dict
) -> None:
    """Expired user lock (locked_until in past, not permanent) does not filter."""
    locks_repo.upsert_user_company_lock(
        db, user_a.id, "acme.com", reason="reply"
    )
    row = locks_repo.get_user_company_lock(db, user_a.id, "acme.com")
    assert row is not None
    row.locked_until = datetime.now(UTC) - timedelta(days=1)
    db.add(row)
    db.commit()

    r = client_factory(user_a).get("/api/v1/contacts")
    emails = {item["email"] for item in r.json()["items"]}
    assert "sarah@acme.com" in emails
