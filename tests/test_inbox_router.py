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


# ─────────────────────────── GET /{id} thread detail ───────────────────────────


def _seed_replied_with_body(
    db: Session,
    *,
    user: User,
    domain: str = "acme.com",
    outbound_subject: str = "Quick intro about ML internship",
    outbound_body: str = "Hi Jane,\n\nI'd love to chat.\n\nThanks,\nMridul",
    reply_body: str | None = "Sure, send me your resume.",
    reply_from: str | None = None,
    explicit_stop: bool = False,
) -> SendQueue:
    """Like _seed_replied but with realistic outbound + inbound bodies stored
    so the detail endpoint has something to render."""
    company = Company(domain=domain, name=domain.split(".")[0].title(), source="test")
    db.add(company)
    db.flush()
    contact = Contact(
        company_id=company.id,
        name="Jane Doe",
        email=f"jane@{domain}",
        role="Recruiter",
    )
    db.add(contact)
    db.flush()
    sq = SendQueue(
        user_id=user.id,
        contact_id=contact.id,
        to_contact_id=contact.id,
        cc_contact_ids="[]",
        company_domain=domain,
        subject=outbound_subject,
        body_text=outbound_body,
        kind="INITIAL",
        scheduled_for=datetime.now(UTC),
        status="REPLIED",
        sent_at=datetime.now(UTC) - timedelta(hours=2),
        replied_at=datetime.now(UTC) - timedelta(minutes=10),
        reply_is_explicit_stop=explicit_stop,
        gmail_message_id="msg-out-1",
        gmail_thread_id="thr-detail-1",
        rfc822_message_id="<original@mail.gmail.com>",
        reply_body_text=reply_body,
        reply_from_email=reply_from or (contact.email if reply_body else None),
        reply_internal_date=(
            datetime.now(UTC) - timedelta(minutes=10) if reply_body else None
        ),
    )
    db.add(sq)
    db.commit()
    return sq


def test_detail_404_when_row_belongs_to_other_user(db: Session, client_factory) -> None:
    """Single 404 covers 'not yours' — no existence side channel."""
    alice = _free_user(db, email="a@x.com", google_sub="g-a")
    bob = _make_user(
        db, email="b@x.com", google_sub="g-b", tier="free", waitlist_email="b@x.com"
    )
    sq = _seed_replied_with_body(db, user=bob)

    r = client_factory(alice).get(f"/api/v1/inbox/{sq.id}")
    assert r.status_code == 404


def test_detail_404_when_id_does_not_exist(db: Session, client_factory) -> None:
    user = _free_user(db)
    r = client_factory(user).get("/api/v1/inbox/99999")
    assert r.status_code == 404


def test_detail_404_when_status_is_sent_not_in_inbox(
    db: Session, client_factory
) -> None:
    """SENT-with-no-reply belongs on /today, not the inbox surface."""
    user = _free_user(db)
    sq = _seed_replied_with_body(db, user=user)
    sq.status = "SENT"
    sq.replied_at = None
    sq.reply_body_text = None
    db.commit()
    r = client_factory(user).get(f"/api/v1/inbox/{sq.id}")
    assert r.status_code == 404


def test_detail_returns_outbound_and_inbound_for_replied_row(
    db: Session, client_factory
) -> None:
    """A REPLIED row with a stored body yields the 2-message mini-thread.
    Shape matches frontend's ThreadDetailSchema exactly — `sender` on the
    detail is the recruiter (ThreadParticipant), each message has `id` +
    `from`, body content lives in `body_html` (pass-through, no conversion)."""
    user = _free_user(db)
    sq = _seed_replied_with_body(
        db,
        user=user,
        outbound_subject="Hello",
        outbound_body="First paragraph.\n\nSecond paragraph.",
        reply_body="Thanks for reaching out.",
    )
    r = client_factory(user).get(f"/api/v1/inbox/{sq.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(sq.id)
    assert body["category"] == "reply"
    assert body["subject"] == "Hello"
    # Recruiter side on detail.sender — name, email, role, company per Zod.
    assert body["sender"]["email"] == "jane@acme.com"
    assert body["sender"]["name"] == "Jane Doe"
    assert body["sender"]["role"] == "Recruiter"
    assert body["sender"]["company"] == "Acme"
    assert body["suggested_followup"] is None
    assert len(body["messages"]) == 2

    outbound = body["messages"][0]
    assert outbound["direction"] == "outbound"
    assert isinstance(outbound["id"], str) and outbound["id"]
    assert outbound["from"]["email"] == user.email
    # Pass-through — backend stores plain text in send_queue.body_text and
    # surfaces it under the frontend's `body_html` field name without any
    # conversion. Frontend owns rendering.
    assert outbound["body_html"] == "First paragraph.\n\nSecond paragraph."

    inbound = body["messages"][1]
    assert inbound["direction"] == "inbound"
    assert inbound["from"]["email"] == "jane@acme.com"
    assert inbound["from"]["name"] == "Jane Doe"
    assert inbound["body_html"] == "Thanks for reaching out."


def test_detail_bounce_shows_only_outbound_and_no_reply(
    db: Session, client_factory
) -> None:
    """Bounced rows have no inbound to show — single-message thread."""
    user = _free_user(db)
    sq = _seed_bounced(db, user=user)
    body = client_factory(user).get(f"/api/v1/inbox/{sq.id}").json()
    assert body["category"] == "bounce"
    assert len(body["messages"]) == 1
    assert body["messages"][0]["direction"] == "outbound"


def test_detail_outbound_reply_surfaces_after_inbound(
    db: Session, client_factory
) -> None:
    """After the user replies from Knock (POST /reply), the next detail render
    must show their outbound reply as a third message."""
    user = _free_user(db)
    sq = _seed_replied_with_body(db, user=user)
    sq.outbound_reply_text = "Here's my resume — link attached."
    sq.outbound_reply_sent_at = datetime.now(UTC) - timedelta(minutes=2)
    db.commit()

    body = client_factory(user).get(f"/api/v1/inbox/{sq.id}").json()
    assert len(body["messages"]) == 3
    assert [m["direction"] for m in body["messages"]] == [
        "outbound",
        "inbound",
        "outbound",
    ]
    assert body["messages"][2]["body_html"] == "Here's my resume — link attached."


def test_detail_passes_through_special_chars_raw(
    db: Session, client_factory
) -> None:
    """Pass-through contract: backend does NOT escape or wrap content. Same as
    /today and /templates — frontend owns rendering. A body containing `<` or
    `&` arrives at the frontend exactly as stored."""
    user = _free_user(db)
    sq = _seed_replied_with_body(
        db, user=user, outbound_body="<script>alert(1)</script> & more"
    )
    body = client_factory(user).get(f"/api/v1/inbox/{sq.id}").json()
    # Untouched — no escape, no wrap, no conversion.
    assert body["messages"][0]["body_html"] == "<script>alert(1)</script> & more"


# ─────────────────────────── POST /{id}/reply ───────────────────────────


def test_reply_404_for_unrelated_row(db: Session, client_factory) -> None:
    alice = _free_user(db, email="a@x.com", google_sub="g-a")
    bob = _make_user(
        db, email="b@x.com", google_sub="g-b", tier="free", waitlist_email="b@x.com"
    )
    sq = _seed_replied_with_body(db, user=bob)
    r = client_factory(alice).post(
        f"/api/v1/inbox/{sq.id}/reply", json={"body_html": "<p>Hi</p>"}
    )
    assert r.status_code == 404


def test_reply_409_when_row_is_bounce(db: Session, client_factory) -> None:
    """Can't reply to a bounce — no human at the other end."""
    user = _free_user(db)
    sq = _seed_bounced(db, user=user)
    r = client_factory(user).post(
        f"/api/v1/inbox/{sq.id}/reply", json={"body_html": "<p>Hi</p>"}
    )
    assert r.status_code == 409


def test_reply_400_when_body_empty(db: Session, client_factory) -> None:
    user = _free_user(db)
    sq = _seed_replied_with_body(db, user=user)
    r = client_factory(user).post(
        f"/api/v1/inbox/{sq.id}/reply", json={"body_html": "   "}
    )
    assert r.status_code == 400


def test_reply_happy_path_persists_outbound_text(
    db: Session, client_factory, monkeypatch
) -> None:
    """Happy path: mocks gmail_send.send_followup → 200 with the new ids; the
    sent body is denormalized on the send_queue row so the detail view shows
    the user's own reply on the next read."""
    from app.routers import inbox as inbox_router
    from app.services import gmail_send

    captured: dict = {}

    def _fake_send_followup(creds, **kwargs):  # noqa: ANN001 — test stub
        captured.update(kwargs)
        return gmail_send.SendResult(
            ok=True,
            gmail_message_id="msg-new-1",
            gmail_thread_id=kwargs["gmail_thread_id"],
            rfc822_message_id="<new@mail.gmail.com>",
        )

    def _fake_creds(_user):
        return object()  # send_followup is mocked → creds value doesn't matter

    monkeypatch.setattr(inbox_router.gmail_send, "send_followup", _fake_send_followup)
    monkeypatch.setattr(inbox_router, "get_user_credentials", _fake_creds)

    user = _free_user(db)
    sq = _seed_replied_with_body(db, user=user)

    r = client_factory(user).post(
        f"/api/v1/inbox/{sq.id}/reply",
        json={"body_html": "<p>Sure, here's my resume.</p>"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Response shape per frontend's ReplyResultSchema: {ok: true, message_id}.
    assert body["ok"] is True
    assert body["message_id"] == "msg-new-1"

    # Threading: must reuse the original gmail_thread_id + rfc822 message id.
    assert captured["gmail_thread_id"] == "thr-detail-1"
    assert captured["in_reply_to_rfc822_id"] == "<original@mail.gmail.com>"
    # To: defaults to the address that wrote back to us.
    assert captured["to_email"] == "jane@acme.com"
    # Subject: Re:-prefixed (original was 'Quick intro about ML internship').
    assert captured["subject"].lower().startswith("re:")

    # Denormalized on the row for the next detail render — pass-through, the
    # column stores whatever the composer sent (Tiptap HTML here).
    db.refresh(sq)
    assert sq.outbound_reply_text == "<p>Sure, here's my resume.</p>"
    assert sq.outbound_reply_sent_at is not None


def test_reply_returns_401_on_gmail_auth_revoked(
    db: Session, client_factory, monkeypatch
) -> None:
    """If get_user_credentials raises OAuthError, surface as 401 so the
    frontend can show a reconnect-Gmail CTA."""
    from app.routers import inbox as inbox_router
    from app.services.google_oauth import OAuthError

    def _fake_creds_raise(_user):
        raise OAuthError("refresh_token missing")

    monkeypatch.setattr(inbox_router, "get_user_credentials", _fake_creds_raise)

    user = _free_user(db)
    sq = _seed_replied_with_body(db, user=user)
    r = client_factory(user).post(
        f"/api/v1/inbox/{sq.id}/reply", json={"body_html": "<p>Hi</p>"}
    )
    assert r.status_code == 401


def test_reply_maps_quota_to_429(db: Session, client_factory, monkeypatch) -> None:
    """gmail_send.send_followup → ok=False / quota_exceeded must surface as 429."""
    from app.routers import inbox as inbox_router
    from app.services import gmail_send

    def _fake_send_followup(_creds, **_kwargs):
        return gmail_send.SendResult(
            ok=False,
            failure_kind="quota_exceeded",
            gmail_error_code="rateLimitExceeded",
            error_message="Rate limit exceeded",
        )

    monkeypatch.setattr(inbox_router.gmail_send, "send_followup", _fake_send_followup)
    monkeypatch.setattr(inbox_router, "get_user_credentials", lambda _u: object())

    user = _free_user(db)
    sq = _seed_replied_with_body(db, user=user)
    r = client_factory(user).post(
        f"/api/v1/inbox/{sq.id}/reply", json={"body_html": "<p>Hi</p>"}
    )
    assert r.status_code == 429


# ─────────────────────────── sync-status ───────────────────────────


def test_sync_status_returns_healthy_true(db: Session, client_factory) -> None:
    """Frontend Zod requires a 'healthy' field — its absence was part of the
    snag. Always true in v0."""
    user = _free_user(db)
    body = client_factory(user).get("/api/v1/inbox/sync-status").json()
    assert body["healthy"] is True
    assert "last_synced_at" in body
