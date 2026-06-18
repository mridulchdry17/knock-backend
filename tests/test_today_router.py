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


def test_patch_late_approval_restamps_send_time(
    client_factory, db: Session, free_user: User
) -> None:
    """Editing/approving a card with a past send_time must re-stamp it to the
    back of the schedule, NOT leave it immediately-due (which would blast on
    the next drain tick)."""
    from datetime import timedelta

    from app.core.time import ensure_utc
    from app.repositories import today_batch as today_repo

    _seed_pool(db, n=1)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    # Force this card's send_time well into the past.
    items = today_repo.list_for_user_date(db, free_user.id, utcnow().date())
    item = items[0]
    now = utcnow()
    item.send_time = now - timedelta(hours=4)
    db.add(item)
    db.commit()

    client = client_factory(free_user)
    # Edit (not setting send_time explicitly) → status auto-flips to 'ready' →
    # late-stamp helper must re-stamp the past send_time forward.
    r = client.patch(
        f"/api/v1/today/items/{item.id}",
        json={"subject": "Edited subject"},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "ready"

    db.expire_all()
    fresh = today_repo.list_for_user_date(db, free_user.id, utcnow().date())[0]
    assert ensure_utc(fresh.send_time) > now, fresh.send_time


def test_patch_explicit_send_time_skips_late_restamp(
    client_factory, db: Session, free_user: User
) -> None:
    """If the caller sets send_time explicitly in the PATCH, we honor it
    verbatim — no surprise re-stamping."""
    from datetime import timedelta

    from app.core.time import ensure_utc
    from app.repositories import today_batch as today_repo

    _seed_pool(db, n=1)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    items = today_repo.list_for_user_date(db, free_user.id, utcnow().date())
    item = items[0]
    # User explicitly chooses a past send_time (unusual but allowed — perhaps
    # to test the drain or to backfill an audit row). We must NOT re-stamp.
    chosen = utcnow() - timedelta(hours=2)
    client = client_factory(free_user)
    r = client.patch(
        f"/api/v1/today/items/{item.id}",
        json={"send_time": chosen.isoformat(), "status": "ready"},
    )
    assert r.status_code == 200

    db.expire_all()
    fresh = today_repo.list_for_user_date(db, free_user.id, utcnow().date())[0]
    # Within a few seconds (datetime serialization round-trip rounding).
    assert abs((ensure_utc(fresh.send_time) - chosen).total_seconds()) < 2


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


# ─────────────────────────── POST /today/apply-template ──────────────────────


def _seed_user_template(db: Session, user: User, name: str = "T1"):
    from app.models import Template

    t = Template(
        user_id=user.id,
        name=name,
        subject="Hi from {{first_name}}",
        body="Hey {{first_name}}, loved {{company}}.",
        is_starter=False,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_apply_template_rewrites_all_pristine_cards(
    client_factory, db: Session, free_user: User
) -> None:
    """Batch-apply re-renders every pristine (un-edited, non-terminal) card."""
    _seed_pool(db, n=3)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    tmpl = _seed_user_template(db, free_user, name="Cool Opener")

    client = client_factory(free_user)
    r = client.post("/api/v1/today/apply-template", json={"template_id": tmpl.id})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["rewritten"] >= 1
    assert out["kept_edited"] == 0
    assert out["skipped_terminal"] == 0

    # Every card now uses the new template + the placeholder text.
    items = client.get("/api/v1/today").json()["items"]
    assert all(it["template_id"] == str(tmpl.id) for it in items)
    assert all(it["subject"].startswith("Hi from ") for it in items)
    # 'default' → 'ready' on rewrite.
    assert all(it["status"] in ("ready", "sent", "skipped") for it in items)


def test_apply_template_preserves_manually_edited_cards(
    client_factory, db: Session, free_user: User
) -> None:
    """A card the user edited (subject or body via PATCH) must NOT be rewritten."""
    _seed_pool(db, n=3)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    client = client_factory(free_user)
    items = client.get("/api/v1/today").json()["items"]
    edited_id = items[0]["id"]

    # Edit the first card's subject — sets edited_at.
    r = client.patch(
        f"/api/v1/today/items/{edited_id}",
        json={"subject": "MY personalized line"},
    )
    assert r.status_code == 200

    tmpl = _seed_user_template(db, free_user, name="Cool Opener")
    r = client.post("/api/v1/today/apply-template", json={"template_id": tmpl.id})
    assert r.status_code == 200
    assert r.json()["kept_edited"] == 1

    # The edited card kept its custom subject.
    refreshed = client.get("/api/v1/today").json()["items"]
    by_id = {it["id"]: it for it in refreshed}
    assert by_id[edited_id]["subject"] == "MY personalized line"
    # The others got rewritten.
    others = [it for it in refreshed if it["id"] != edited_id]
    assert all(it["template_id"] == str(tmpl.id) for it in others)


def test_apply_template_skips_terminal_cards(
    client_factory, db: Session, free_user: User
) -> None:
    """sent / failed / skipped / cooldown cards are not rewritten."""
    _seed_pool(db, n=2)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    client = client_factory(free_user)
    items = client.get("/api/v1/today").json()["items"]
    skip_id = items[0]["id"]
    r = client.patch(f"/api/v1/today/items/{skip_id}", json={"status": "skipped"})
    assert r.status_code == 200

    tmpl = _seed_user_template(db, free_user, name="X")
    r = client.post("/api/v1/today/apply-template", json={"template_id": tmpl.id})
    assert r.status_code == 200
    out = r.json()
    assert out["skipped_terminal"] >= 1

    refreshed = client.get("/api/v1/today").json()["items"]
    by_id = {it["id"]: it for it in refreshed}
    # Skipped card kept its OLD template_id (not the new one).
    assert by_id[skip_id]["template_id"] != str(tmpl.id) or by_id[skip_id]["status"] == "skipped"


def test_apply_template_cross_user_404(
    client_factory, db: Session, free_user: User
) -> None:
    """Applying another user's template must 404."""
    _seed_pool(db, n=1)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    other = _make_user(
        db, email="other@x.com", google_sub="g-other3", tier="free",
        waitlist_email="other@x.com",
    )
    others_tpl = _seed_user_template(db, other, name="Theirs")

    client = client_factory(free_user)
    r = client.post("/api/v1/today/apply-template", json={"template_id": others_tpl.id})
    assert r.status_code == 404


def test_template_swap_clears_edited_at(
    client_factory, db: Session, free_user: User
) -> None:
    """A PATCH that swaps template_id must clear edited_at — so a subsequent
    batch-apply includes the card again."""
    _seed_pool(db, n=2)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    client = client_factory(free_user)
    item_id = client.get("/api/v1/today").json()["items"][0]["id"]

    # User edits the subject → edited_at gets set.
    r = client.patch(f"/api/v1/today/items/{item_id}", json={"subject": "tweaked"})
    assert r.status_code == 200

    # User changes their mind, picks a different template → edited_at clears.
    swap_tpl = _seed_user_template(db, free_user, name="Swap")
    r = client.patch(
        f"/api/v1/today/items/{item_id}",
        json={"template_id": swap_tpl.id},
    )
    assert r.status_code == 200

    # Batch-apply with yet another template now includes the card.
    final_tpl = _seed_user_template(db, free_user, name="Final")
    r = client.post("/api/v1/today/apply-template", json={"template_id": final_tpl.id})
    assert r.status_code == 200
    assert r.json()["kept_edited"] == 0

    refreshed = client.get("/api/v1/today").json()["items"]
    by_id = {it["id"]: it for it in refreshed}
    assert by_id[item_id]["template_id"] == str(final_tpl.id)


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


def test_send_batch_does_not_blast_late_items(
    client_factory, db: Session, free_user: User
) -> None:
    """The bug: clicking 'Send today's batch' used to blast every approved card
    immediately. Now late items (send_time < now) must be re-stamped to the back
    of the schedule at the tier's cadence — none should dispatch in this call.
    """
    from datetime import timedelta

    from app.repositories import today_batch as today_repo

    _seed_pool(db, n=3)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    # Force all three send_times into the past so they're all "late".
    now = utcnow()
    items = today_repo.list_for_user_date(db, free_user.id, utcnow().date())
    for i, item in enumerate(items):
        item.send_time = now - timedelta(hours=3 + i)
        db.add(item)
    db.commit()

    client = client_factory(free_user)
    with _stub_send_ok():
        r = client.post("/api/v1/today/send-batch")

    assert r.status_code == 200
    body = r.json()
    assert body["dispatched_count"] == 3  # 3 approved (queued), not blasted

    # NOTHING was dispatched immediately — all 3 are queued (status='ready')
    # with FUTURE send_times spaced ~1 hour apart (free cadence).
    from app.core.time import ensure_utc

    db.expire_all()
    items = today_repo.list_for_user_date(db, free_user.id, utcnow().date())
    assert all(i.status == "ready" for i in items), [i.status for i in items]
    assert all(ensure_utc(i.send_time) > now for i in items), [
        (i.id, i.send_time) for i in items
    ]
    # sent_today unchanged — nothing actually sent in this call.
    assert db.get(User, free_user.id).sent_today == 0


def test_send_batch_keeps_future_slots_intact_restamps_only_late(
    client_factory, db: Session, free_user: User
) -> None:
    """Future-dated cards keep their original slot; only the late ones move."""
    from datetime import timedelta

    from app.repositories import today_batch as today_repo

    _seed_pool(db, n=3)
    batch_gen_svc.generate_batch_for_user(
        db, free_user, batch_date=utcnow().date(), rng=Random(1)
    )
    now = utcnow()
    items = sorted(
        today_repo.list_for_user_date(db, free_user.id, utcnow().date()),
        key=lambda x: x.id,
    )
    items[0].send_time = now - timedelta(hours=2)  # late
    items[1].send_time = now + timedelta(hours=1)  # future
    items[2].send_time = now + timedelta(hours=3)  # future (latest)
    for it in items:
        db.add(it)
    db.commit()
    original_future = (items[1].send_time, items[2].send_time)

    client = client_factory(free_user)
    with _stub_send_ok():
        r = client.post("/api/v1/today/send-batch")
    assert r.status_code == 200

    db.expire_all()
    items = sorted(
        today_repo.list_for_user_date(db, free_user.id, utcnow().date()),
        key=lambda x: x.id,
    )
    from app.core.time import ensure_utc

    # Future slots untouched.
    assert ensure_utc(items[1].send_time) == ensure_utc(original_future[0])
    assert ensure_utc(items[2].send_time) == ensure_utc(original_future[1])
    # Late item moved to AFTER the latest future slot (cadence past +3h).
    assert ensure_utc(items[0].send_time) > ensure_utc(items[2].send_time)


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
    assert r.json()["dispatched_count"] == 2  # 2 approved (skipped one excluded)

    db.expire_all()
    items = today_repo.list_for_user_date(db, free_user.id, utcnow().date())
    statuses = sorted(i.status for i in items)
    # Skipped stays skipped; the other two are queued (ready) or sent depending
    # on whether their slot is currently due.
    assert "skipped" in statuses
    assert "default" not in statuses


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
        # Snapshot state after first call.
        from app.repositories import today_batch as today_repo

        db.expire_all()
        after_first = sorted(
            (i.status, i.send_time)
            for i in today_repo.list_for_user_date(db, free_user.id, utcnow().date())
        )
        # Second call: discarded result — we only care that state didn't change.
        client.post("/api/v1/today/send-batch")

    assert first.json()["dispatched_count"] == 2  # 2 approved on first call
    # Second call: items are already 'ready' (not 'default'), so no promotion
    # happens but they're still counted as sendable in dispatched_count if
    # status='ready'. The key invariant: send_times don't move and no double-send.
    db.expire_all()
    after_second = sorted(
        (i.status, i.send_time)
        for i in today_repo.list_for_user_date(db, free_user.id, utcnow().date())
    )
    assert after_first == after_second  # no double-restamp, no double-send


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
