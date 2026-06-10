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


def test_patch_template_id_rerenders_subject_and_body(
    client_factory, db: Session, free_user: User
) -> None:
    """Picking a new template via PATCH must re-render the card's subject/body
    using the template's placeholders against the card's recipient — NOT just
    swap the FK."""
    from app.models import Template

    _seed_pool(db, n=1)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    # Custom template owned by this user.
    tmpl = Template(
        user_id=free_user.id,
        name="Cool Opener",
        subject="Hi from {{first_name}}",
        body="Hey {{first_name}}, loved {{company}}.",
        is_starter=False,
    )
    db.add(tmpl)
    db.commit()

    client = client_factory(free_user)
    item_id = client.get("/api/v1/today").json()["items"][0]["id"]

    r = client.patch(
        f"/api/v1/today/items/{item_id}",
        json={"template_id": tmpl.id},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["template_id"] == str(tmpl.id)
    assert out["template_name"] == "Cool Opener"
    # Placeholders rendered with the contact's actual first name + company.
    assert "Hey User" in out["body"] or out["body"].startswith("Hey ")
    assert "loved Co " in out["body"] or "loved " in out["body"]
    # Editing the template auto-promotes status to 'ready'.
    assert out["status"] == "ready"


def test_patch_template_id_belonging_to_another_user_404(
    client_factory, db: Session, free_user: User
) -> None:
    """Cross-user template use must 404 — don't leak existence of another
    user's templates via 403."""
    from app.models import Template

    _seed_pool(db, n=1)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    other = _make_user(
        db, email="other@x.com", google_sub="g-other2", tier="free",
        waitlist_email="other@x.com",
    )
    other_template = Template(
        user_id=other.id, name="Theirs", subject="x", body="y", is_starter=False,
    )
    db.add(other_template)
    db.commit()

    client = client_factory(free_user)
    item_id = client.get("/api/v1/today").json()["items"][0]["id"]

    r = client.patch(
        f"/api/v1/today/items/{item_id}",
        json={"template_id": other_template.id},
    )
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


# ─────────────────────────── POST /today/send-batch ───────────────────────────


def _stub_send_ok():
    """Patch gmail_send.send_email + creds so drain_due_items "sends" cleanly.

    Module-level patches, so they apply regardless of which DB session the
    router uses internally.
    """
    from contextlib import ExitStack
    from unittest.mock import patch

    from app.services import gmail_send, send_worker

    stack = ExitStack()
    stack.enter_context(
        patch.object(
            gmail_send,
            "send_email",
            return_value=gmail_send.SendResult(
                ok=True, gmail_message_id="m", gmail_thread_id="t"
            ),
        )
    )
    stack.enter_context(
        patch.object(send_worker, "get_user_credentials", return_value=object())
    )
    return stack


def test_send_batch_dispatches_non_skipped_items(
    client_factory, db: Session, free_user: User
) -> None:
    _seed_pool(db, n=3)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    client = client_factory(free_user)

    with _stub_send_ok():
        r = client.post("/api/v1/today/send-batch")

    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {
        "dispatched_count",
        "scheduled_first_at",
        "scheduled_last_at",
    }
    assert body["dispatched_count"] == 3

    # All three cards transitioned to 'sent'.
    from app.repositories import today_batch as today_repo

    db.expire_all()
    items = today_repo.list_for_user_date(db, free_user.id, utcnow().date())
    assert sorted(i.status for i in items) == ["sent", "sent", "sent"]
    # sent_today bumped.
    assert db.get(User, free_user.id).sent_today == 3


def test_send_batch_leaves_skipped_items_untouched(
    client_factory, db: Session, free_user: User
) -> None:
    _seed_pool(db, n=3)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    from app.repositories import today_batch as today_repo

    items = today_repo.list_for_user_date(db, free_user.id, utcnow().date())
    items[0].status = "skipped"
    db.add(items[0])
    db.commit()

    client = client_factory(free_user)
    with _stub_send_ok():
        r = client.post("/api/v1/today/send-batch")

    assert r.status_code == 200
    assert r.json()["dispatched_count"] == 2

    db.expire_all()
    items = today_repo.list_for_user_date(db, free_user.id, utcnow().date())
    statuses = sorted(i.status for i in items)
    assert statuses == ["sent", "sent", "skipped"]


def test_send_batch_is_idempotent(
    client_factory, db: Session, free_user: User
) -> None:
    _seed_pool(db, n=2)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    client = client_factory(free_user)

    with _stub_send_ok():
        first = client.post("/api/v1/today/send-batch")
        second = client.post("/api/v1/today/send-batch")

    assert first.json()["dispatched_count"] == 2
    # Everything already sent → nothing to dispatch on the second call.
    assert second.json()["dispatched_count"] == 0


def test_send_batch_with_no_batch_dispatches_zero(
    client_factory, free_user: User
) -> None:
    # No batch generated → GET would lazy-gen, but send-batch reads whatever
    # exists. With no contacts seeded there's nothing to send.
    client = client_factory(free_user)
    with _stub_send_ok():
        r = client.post("/api/v1/today/send-batch")
    assert r.status_code == 200
    assert r.json()["dispatched_count"] == 0


def test_send_batch_requires_auth(client_factory) -> None:
    client = client_factory(None)
    r = client.post("/api/v1/today/send-batch")
    assert r.status_code == 401


# ─────────────────────────── POST /today/skip ───────────────────────────


def test_skip_today_flips_all_pending_to_skipped(
    client_factory, db: Session, free_user: User
) -> None:
    _seed_pool(db, n=3)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    client = client_factory(free_user)

    r = client.post("/api/v1/today/skip")
    assert r.status_code == 200
    assert r.json() == {"skipped": True}

    from app.repositories import today_batch as today_repo

    db.expire_all()
    items = today_repo.list_for_user_date(db, free_user.id, utcnow().date())
    assert all(i.status == "skipped" for i in items)


def test_skip_today_leaves_sent_cards_untouched(
    client_factory, db: Session, free_user: User
) -> None:
    _seed_pool(db, n=2)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    from app.repositories import today_batch as today_repo

    items = today_repo.list_for_user_date(db, free_user.id, utcnow().date())
    items[0].status = "sent"
    db.add(items[0])
    db.commit()

    client = client_factory(free_user)
    r = client.post("/api/v1/today/skip")
    assert r.status_code == 200

    db.expire_all()
    items = today_repo.list_for_user_date(db, free_user.id, utcnow().date())
    assert sorted(i.status for i in items) == ["sent", "skipped"]


def test_skip_today_is_idempotent(
    client_factory, db: Session, free_user: User
) -> None:
    _seed_pool(db, n=2)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    client = client_factory(free_user)
    assert client.post("/api/v1/today/skip").json() == {"skipped": True}
    # Second call: nothing pending, still succeeds.
    assert client.post("/api/v1/today/skip").json() == {"skipped": True}


def test_skip_today_requires_auth(client_factory) -> None:
    client = client_factory(None)
    r = client.post("/api/v1/today/skip")
    assert r.status_code == 401


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
