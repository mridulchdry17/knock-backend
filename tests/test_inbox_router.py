"""HTTP tests for /api/v1/inbox (B5.6).

Mirrors the test_today_router client_factory pattern so we exercise the real
router + dependency stack with mocked auth/db.
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
from app.models import Company, Contact, SendQueue, User
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


def _seed_replied(
    db: Session,
    *,
    user: User,
    domain: str = "acme.com",
    company_name: str = "Acme",
    explicit_stop: bool = False,
    replied_minutes_ago: int = 5,
) -> SendQueue:
    company = Company(domain=domain, name=company_name, source="test")
    db.add(company)
    db.flush()
    contact = Contact(
        company_id=company.id,
        name="John Doe",
        email=f"john@{domain}",
        role="Engineer",
    )
    db.add(contact)
    db.flush()
    sq = SendQueue(
        user_id=user.id,
        contact_id=contact.id,
        to_contact_id=contact.id,
        cc_contact_ids="[]",
        company_domain=domain,
        subject="Re: Quick intro",
        body_text="...",
        kind="INITIAL",
        scheduled_for=datetime.now(UTC),
        status="REPLIED",
        sent_at=datetime.now(UTC) - timedelta(hours=1),
        replied_at=datetime.now(UTC) - timedelta(minutes=replied_minutes_ago),
        reply_is_explicit_stop=explicit_stop,
        gmail_message_id="msg-out-1",
        gmail_thread_id="thr-1",
    )
    db.add(sq)
    db.commit()
    return sq


def _free_user(db: Session, email: str = "u@x.com", google_sub: str = "g-1") -> User:
    return _make_user(
        db, email=email, google_sub=google_sub, tier="free", waitlist_email=email
    )


# ─────────────────────────── gating ───────────────────────────


def test_unauthenticated_is_401(client_factory) -> None:
    client = client_factory(None)
    r = client.get("/api/v1/inbox")
    assert r.status_code == 401


def test_pending_tier_is_403(db: Session, client_factory) -> None:
    user = _make_user(db, email="p@x.com", tier="pending")
    client = client_factory(user)
    r = client.get("/api/v1/inbox")
    assert r.status_code == 403


# ─────────────────────────── empty + populated ───────────────────────────


def test_empty_returns_200_not_404(db: Session, client_factory) -> None:
    user = _free_user(db)
    client = client_factory(user)
    r = client.get("/api/v1/inbox")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_lists_user_replies_newest_first(db: Session, client_factory) -> None:
    user = _free_user(db)
    _seed_replied(db, user=user, domain="acme.com", company_name="Acme", replied_minutes_ago=30)
    _seed_replied(db, user=user, domain="beta.io", company_name="Beta", replied_minutes_ago=5)

    client = client_factory(user)
    r = client.get("/api/v1/inbox")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    domains = [i["company_domain"] for i in body["items"]]
    assert domains == ["beta.io", "acme.com"]  # newest first


def test_explicit_stop_field_surfaces_platform_permanent_status(
    db: Session, client_factory
) -> None:
    user = _free_user(db)
    # Simulating the post-ingest world: send_queue row + platform_company_lock row.
    _seed_replied(db, user=user, explicit_stop=True)
    from app.models import PlatformCompanyLock

    db.add(
        PlatformCompanyLock(
            company_domain="acme.com",
            reason="explicit_stop_reply",
            created_at=datetime.now(UTC),
        )
    )
    db.commit()

    client = client_factory(user)
    r = client.get("/api/v1/inbox")
    item = r.json()["items"][0]
    assert item["reply_is_explicit_stop"] is True
    assert item["lock_status"] == "platform_permanent"
    assert item["locked_until"] is None


def test_regular_reply_surfaces_user_reply_lock_status(
    db: Session, client_factory
) -> None:
    user = _free_user(db)
    _seed_replied(db, user=user, explicit_stop=False)
    # Per-user lock — what record_reply_from_company writes.
    from app.models import UserCompanyLock

    db.add(
        UserCompanyLock(
            user_id=user.id,
            company_domain="acme.com",
            locked_at=datetime.now(UTC),
            locked_until=datetime.now(UTC) + timedelta(days=30),
            is_permanent=False,
            reason="reply",
        )
    )
    db.commit()

    client = client_factory(user)
    item = client.get("/api/v1/inbox").json()["items"][0]
    assert item["lock_status"] == "user_reply_lock"
    assert item["locked_until"] is not None


def test_user_isolation(db: Session, client_factory) -> None:
    """User A must not see User B's replies."""
    alice = _free_user(db, email="a@x.com", google_sub="g-a")
    _bob = _make_user(
        db, email="b@x.com", google_sub="g-b", tier="free", waitlist_email="b@x.com"
    )
    _seed_replied(db, user=_bob, domain="bobcorp.com", company_name="BobCorp")

    client = client_factory(alice)
    body = client.get("/api/v1/inbox").json()
    assert body["items"] == []
    assert body["total"] == 0
