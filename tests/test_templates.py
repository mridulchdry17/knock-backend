"""Tests for the templates feature: render, seeding, CRUD + cap, router.

Render and seeding are service-level (no HTTP); CRUD/cap/gating go through the
real router via the client_factory pattern used elsewhere.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.deps import get_current_user
from app.db.session import get_db
from app.main import app
from app.models import Company, Contact, Template, User
from app.services import templates as templates_svc
from tests.conftest import _make_user

# ─────────────────────────── render ───────────────────────────


def _contact(name: str | None, role: str | None) -> Contact:
    return Contact(name=name, role=role, email="x@acme.com")


def _company(name: str) -> Company:
    return Company(name=name, domain="acme.com", source="t")


def test_render_fills_all_placeholders() -> None:
    subj, body = templates_svc.render_template(
        "Hi from a student exploring {{company}}",
        "Hi {{first_name}}, you're {{role}} at {{company}}. — {{sender_name}}",
        to_contact=_contact("Akanksha Puri", "Director HR"),
        company=_company("SourceFuse"),
        sender_name="Mridul Chaudhary",
    )
    assert subj == "Hi from a student exploring SourceFuse"
    assert body == "Hi Akanksha, you're Director HR at SourceFuse. — Mridul Chaudhary"


def test_render_uses_fallbacks_for_missing_data() -> None:
    _subj, body = templates_svc.render_template(
        "x",
        "Hi {{first_name}}, {{role}} at {{company}}. — {{sender_name}}",
        to_contact=_contact(None, None),
        company=None,
        sender_name=None,
    )
    assert body == "Hi there, your team at your company. — a student"


def test_render_leaves_unknown_tokens_visible() -> None:
    _subj, body = templates_svc.render_template(
        "x", "Ref: {{unknown_token}}", to_contact=None, company=None, sender_name="A"
    )
    assert body == "Ref: {{unknown_token}}"


def test_render_hr_name_is_full_contact_name() -> None:
    _subj, body = templates_svc.render_template(
        "x", "Dear {{hr_name}}", to_contact=_contact("Akanksha Puri", None),
        company=None, sender_name="A",
    )
    assert body == "Dear Akanksha Puri"


def test_render_flattens_rich_text_html_body_to_plain_text() -> None:
    # The rich-text editor stores the body as HTML, including variable spans.
    # render must substitute placeholders AND flatten to plain text so the
    # text/plain email doesn't show literal <p> tags (the reported bug).
    html_body = (
        '<p>Hi <span data-variable="first_name">{{first_name}}</span>,</p>'
        "<p>I'm a student really interested in "
        '<span data-variable="company">{{company}}</span>.</p>'
        "<p>Best,<br>{{sender_name}}</p>"
    )
    _subj, body = templates_svc.render_template(
        "Hello {{company}}",
        html_body,
        to_contact=_contact("Alex Rivera", "Recruiter"),
        company=_company("Acme Inc"),
        sender_name="Mridul Chaudhary",
    )
    assert "<p>" not in body and "</p>" not in body and "<span" not in body
    assert body == (
        "Hi Alex,\n\n"
        "I'm a student really interested in Acme Inc.\n\n"
        "Best,\nMridul Chaudhary"
    )


# ─────────────────────────── seeding ───────────────────────────


def test_seed_starters_creates_three(db: Session) -> None:
    user = _make_user(db, email="u@x.com", tier="free")
    created = templates_svc.seed_starters(db, user)
    db.commit()
    assert created == 3
    rows = templates_svc.templates_repo.list_for_user(db, user.id)
    assert len(rows) == 3
    assert all(t.is_starter for t in rows)


def test_seed_starters_is_idempotent(db: Session) -> None:
    user = _make_user(db, email="u@x.com", tier="free")
    templates_svc.seed_starters(db, user)
    db.commit()
    second = templates_svc.seed_starters(db, user)
    db.commit()
    assert second == 0
    assert templates_svc.templates_repo.count_for_user(db, user.id) == 3


# ─────────────────────────── router (CRUD + cap + gating) ───────────────────────────


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
                    "unauthorized", "Not authenticated",
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )
            return user

        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[get_current_user] = _override_get_current_user
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def _free_user(db: Session) -> User:
    return _make_user(db, email="u@x.com", tier="free", waitlist_email="u@x.com")


def test_pending_tier_is_403(db: Session, client_factory) -> None:
    user = _make_user(db, email="p@x.com", tier="pending")
    assert client_factory(user).get("/api/v1/templates").status_code == 403


def test_list_empty(db: Session, client_factory) -> None:
    user = _free_user(db)
    r = client_factory(user).get("/api/v1/templates")
    assert r.status_code == 200
    body = r.json()
    assert body == {"items": [], "count": 0, "cap": 3}


def test_create_then_list(db: Session, client_factory) -> None:
    user = _free_user(db)
    client = client_factory(user)
    r = client.post(
        "/api/v1/templates",
        json={"name": "Mine", "subject": "Hi {{first_name}}", "body": "Hello {{company}}"},
    )
    assert r.status_code == 201
    t = r.json()
    assert t["name"] == "Mine"
    assert t["is_starter"] is False
    assert t["used_count"] == 0
    assert t["reply_rate"] is None

    lst = client.get("/api/v1/templates").json()
    assert lst["count"] == 1
    assert lst["cap"] == 3


def test_cap_enforced_at_three(db: Session, client_factory) -> None:
    user = _free_user(db)
    client = client_factory(user)
    for i in range(3):
        assert client.post(
            "/api/v1/templates",
            json={"name": f"T{i}", "subject": "s", "body": "b"},
        ).status_code == 201
    # 4th is rejected.
    r = client.post(
        "/api/v1/templates", json={"name": "T4", "subject": "s", "body": "b"}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "template_limit_reached"


def test_patch_updates_fields(db: Session, client_factory) -> None:
    user = _free_user(db)
    client = client_factory(user)
    tid = client.post(
        "/api/v1/templates", json={"name": "A", "subject": "s", "body": "b"}
    ).json()["id"]

    r = client.patch(f"/api/v1/templates/{tid}", json={"name": "B"})
    assert r.status_code == 200
    assert r.json()["name"] == "B"
    assert r.json()["subject"] == "s"  # unchanged


def test_delete(db: Session, client_factory) -> None:
    user = _free_user(db)
    client = client_factory(user)
    tid = client.post(
        "/api/v1/templates", json={"name": "A", "subject": "s", "body": "b"}
    ).json()["id"]

    assert client.delete(f"/api/v1/templates/{tid}").status_code == 200
    assert client.get("/api/v1/templates").json()["count"] == 0


def test_cannot_touch_another_users_template(db: Session, client_factory) -> None:
    owner = _make_user(db, email="owner@x.com", google_sub="g-o", tier="free")
    t = Template(user_id=owner.id, name="O", subject="s", body="b")
    db.add(t)
    db.commit()

    intruder = _make_user(db, email="intruder@x.com", google_sub="g-i", tier="free")
    r = client_factory(intruder).patch(f"/api/v1/templates/{t.id}", json={"name": "hax"})
    assert r.status_code == 404  # 404 not 403 — don't leak existence


def test_create_validates_blank_and_oversize(db: Session, client_factory) -> None:
    user = _free_user(db)
    client = client_factory(user)
    # blank name
    assert client.post(
        "/api/v1/templates", json={"name": "", "subject": "s", "body": "b"}
    ).status_code == 422
    # oversize body
    assert client.post(
        "/api/v1/templates",
        json={"name": "n", "subject": "s", "body": "x" * 20_001},
    ).status_code == 422
