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


def test_lists_user_replies_newest_first_with_new_shape(
    db: Session, client_factory
) -> None:
    """List response matches the F.7 frontend Zod contract: id is a string,
    sender is an object, category='reply', ordered newest-first. The previous
    shape (company_domain / lock_status / reply_is_explicit_stop) was nowhere
    in the frontend Zod and caused the 'snag' on every tab."""
    user = _free_user(db)
    _seed_replied(db, user=user, domain="acme.com", company_name="Acme", replied_minutes_ago=30)
    _seed_replied(db, user=user, domain="beta.io", company_name="Beta", replied_minutes_ago=5)

    client = client_factory(user)
    r = client.get("/api/v1/inbox")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert body["unread_count"] == 0

    # Shape — exactly what the frontend Zod expects, no extras it doesn't know.
    items = body["items"]
    expected_keys = {
        "id",
        "category",
        "subject",
        "sender",
        "snippet",
        "last_message_at",
        "unread",
        "message_count",
    }
    assert set(items[0].keys()) >= expected_keys
    assert isinstance(items[0]["id"], str)
    assert items[0]["category"] == "reply"
    assert set(items[0]["sender"].keys()) == {"name", "email"}
    # Newest first — beta first since replied 5 min ago vs acme 30 min ago.
    emails = [i["sender"]["email"] for i in items]
    assert emails == ["john@beta.io", "john@acme.com"]


def test_snippet_is_truncated_to_140_chars(db: Session, client_factory) -> None:
    """Snippet is a single-line preview capped at 140 chars."""
    user = _free_user(db)
    sq = _seed_replied(db, user=user)
    sq.body_text = "x" * 300
    db.commit()
    client = client_factory(user)
    item = client.get("/api/v1/inbox").json()["items"][0]
    assert len(item["snippet"]) <= 141  # 140 chars + ellipsis fits


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


# ─────────────────────────── ?category= filter ───────────────────────────


def _seed_bounced(
    db: Session, *, user: User, domain: str = "dead.com", minutes_ago: int = 10
) -> SendQueue:
    """Seed a BOUNCED send_queue row (the reply ingestor's bounce path)."""
    company = Company(domain=domain, name=domain, source="test")
    db.add(company)
    db.flush()
    contact = Contact(company_id=company.id, name=None, email=f"missing@{domain}")
    db.add(contact)
    db.flush()
    sq = SendQueue(
        user_id=user.id,
        contact_id=contact.id,
        to_contact_id=contact.id,
        cc_contact_ids="[]",
        company_domain=domain,
        subject="Re: Quick intro",
        body_text="Delivery failed.",
        kind="INITIAL",
        scheduled_for=datetime.now(UTC),
        status="BOUNCED",
        sent_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        gmail_message_id="msg-out-b",
        gmail_thread_id="thr-b",
    )
    db.add(sq)
    db.commit()
    return sq


def test_category_reply_filters_to_replied_only(db: Session, client_factory) -> None:
    user = _free_user(db)
    _seed_replied(db, user=user, domain="acme.com")
    _seed_bounced(db, user=user, domain="dead.com")

    body = client_factory(user).get("/api/v1/inbox?category=reply").json()
    assert body["total"] == 1
    assert {i["category"] for i in body["items"]} == {"reply"}


def test_category_bounce_filters_to_bounced_only(db: Session, client_factory) -> None:
    """Section 3 (Bounces tab) — was snagging before; now returns BOUNCED rows."""
    user = _free_user(db)
    _seed_replied(db, user=user, domain="acme.com")
    _seed_bounced(db, user=user, domain="dead.com")

    body = client_factory(user).get("/api/v1/inbox?category=bounce").json()
    assert body["total"] == 1
    assert {i["category"] for i in body["items"]} == {"bounce"}


def test_category_nudge_returns_empty_200(db: Session, client_factory) -> None:
    """Nudge category isn't implemented yet — must return empty 200, not snag."""
    user = _free_user(db)
    _seed_replied(db, user=user)

    r = client_factory(user).get("/api/v1/inbox?category=nudge")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["unread_count"] == 0


def test_all_category_includes_both_replies_and_bounces(
    db: Session, client_factory
) -> None:
    """Omitting the category param (All tab) returns replies + bounces."""
    user = _free_user(db)
    _seed_replied(db, user=user, domain="acme.com", replied_minutes_ago=30)
    _seed_bounced(db, user=user, domain="dead.com", minutes_ago=5)

    body = client_factory(user).get("/api/v1/inbox").json()
    assert body["total"] == 2
    cats = [i["category"] for i in body["items"]]
    assert sorted(cats) == ["bounce", "reply"]
    # Newest first — the bounce was 5 min ago vs reply 30 min ago.
    assert body["items"][0]["category"] == "bounce"


# ─────────────────────────── sync-status ───────────────────────────


def test_sync_status_returns_healthy_true(db: Session, client_factory) -> None:
    """Frontend Zod requires a 'healthy' field — its absence was part of the
    snag. Always true in v0."""
    user = _free_user(db)
    body = client_factory(user).get("/api/v1/inbox/sync-status").json()
    assert body["healthy"] is True
    assert "last_synced_at" in body
