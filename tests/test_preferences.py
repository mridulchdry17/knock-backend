"""Tests for the preferences router + service.

Patterns follow tests/test_admin.py: TestClient with `get_db` and
`get_current_user` overridden so we focus on preferences logic, not the
session/bearer plumbing.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import Depends, status
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm import Session as OrmSession

from app.core.deps import get_current_user
from app.core.errors import ApiError
from app.core.time import utcnow
from app.db.session import get_db
from app.main import app
from app.models import User
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

        # Override depends on `get_db` (post-override) so FastAPI resolves the
        # session first; we then re-fetch the user against that exact session.
        # Without this, mutating the fixture-bound user inside a router raises
        # "attached to other session".
        def _override_get_current_user(
            db: OrmSession = Depends(get_db),  # noqa: B008
        ) -> User:
            if user is None:
                raise ApiError(
                    "unauthorized",
                    "Not authenticated",
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )
            return db.get(User, user.id)

        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[get_current_user] = _override_get_current_user
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


@pytest.fixture
def free_user(db: Session) -> User:
    return _make_user(
        db,
        email="free@example.com",
        google_sub="g-free",
        tier="free",
        waitlist_email="free@example.com",
    )


@pytest.fixture
def paid_user(db: Session) -> User:
    return _make_user(
        db,
        email="paid@example.com",
        google_sub="g-paid",
        tier="paid",
        waitlist_email="paid@example.com",
    )


# ─────────────────────────── GET / PATCH preferences ───────────────────────────


def test_get_preferences_returns_defaults_for_new_user(client_factory, free_user) -> None:
    client = client_factory(free_user)
    r = client.get("/api/v1/preferences")
    assert r.status_code == 200
    body = r.json()
    assert body["target_role"] is None
    assert body["target_industries"] == []
    assert body["target_location"] is None
    assert body["notify_gmail_disconnect"] is True
    assert body["notify_daily_summary"] is True
    assert body["autopilot_enabled"] is False
    assert body["autopilot_paused_at"] is None
    assert body["autopilot_auto_pause_on_reply"] is True
    assert body["resume_url"] is None


def test_patch_preferences_updates_atomically(client_factory, free_user) -> None:
    client = client_factory(free_user)
    r = client.patch(
        "/api/v1/preferences",
        json={"target_role": "Backend Engineer", "target_location": "Remote"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["target_role"] == "Backend Engineer"
    assert body["target_location"] == "Remote"
    # Untouched fields stay at defaults.
    assert body["notify_gmail_disconnect"] is True

    # Second patch — only one field. Other field must survive.
    r2 = client.patch("/api/v1/preferences", json={"target_location": "NYC"})
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["target_role"] == "Backend Engineer"
    assert body2["target_location"] == "NYC"


def test_patch_preferences_persists_target_industries(client_factory, free_user) -> None:
    client = client_factory(free_user)
    industries = ["fintech", "developer-tools", "ai/ml"]
    r = client.patch("/api/v1/preferences", json={"target_industries": industries})
    assert r.status_code == 200
    assert r.json()["target_industries"] == industries

    # Round-trip survives a fresh GET.
    r2 = client.get("/api/v1/preferences")
    assert r2.json()["target_industries"] == industries


def test_patch_preferences_clears_field_with_null(client_factory, free_user) -> None:
    client = client_factory(free_user)
    client.patch("/api/v1/preferences", json={"target_role": "SWE"})
    r = client.patch("/api/v1/preferences", json={"target_role": None})
    assert r.status_code == 200
    assert r.json()["target_role"] is None


# ─────────────────────────── excluded domains ───────────────────────────


def test_add_excluded_domain(client_factory, free_user) -> None:
    client = client_factory(free_user)
    r = client.post("/api/v1/preferences/excluded-domains", json={"domain": "acme.com"})
    assert r.status_code == 201
    body = r.json()
    assert body["domain"] == "acme.com"
    assert "created_at" in body


def test_add_excluded_domain_normalizes_leading_at(client_factory, free_user) -> None:
    client = client_factory(free_user)
    r = client.post(
        "/api/v1/preferences/excluded-domains", json={"domain": "@acme.com"}
    )
    assert r.status_code == 201
    assert r.json()["domain"] == "acme.com"


def test_add_excluded_domain_normalizes_case(client_factory, free_user) -> None:
    client = client_factory(free_user)
    r = client.post(
        "/api/v1/preferences/excluded-domains", json={"domain": "Acme.COM"}
    )
    assert r.status_code == 201
    assert r.json()["domain"] == "acme.com"


def test_add_excluded_domain_invalid_format(client_factory, free_user) -> None:
    client = client_factory(free_user)
    r = client.post(
        "/api/v1/preferences/excluded-domains", json={"domain": "not a domain"}
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_domain"


def test_add_excluded_domain_duplicate(client_factory, free_user) -> None:
    client = client_factory(free_user)
    client.post("/api/v1/preferences/excluded-domains", json={"domain": "acme.com"})
    r = client.post("/api/v1/preferences/excluded-domains", json={"domain": "acme.com"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "already_excluded"


def test_remove_excluded_domain(client_factory, free_user) -> None:
    client = client_factory(free_user)
    client.post("/api/v1/preferences/excluded-domains", json={"domain": "acme.com"})
    r = client.delete("/api/v1/preferences/excluded-domains/acme.com")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    list_r = client.get("/api/v1/preferences/excluded-domains")
    assert list_r.json()["items"] == []


def test_remove_excluded_domain_not_found(client_factory, free_user) -> None:
    client = client_factory(free_user)
    r = client.delete("/api/v1/preferences/excluded-domains/ghost.com")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_list_excluded_domains_ordered(client_factory, free_user) -> None:
    client = client_factory(free_user)
    for domain in ["alpha.com", "beta.com", "gamma.com"]:
        r = client.post("/api/v1/preferences/excluded-domains", json={"domain": domain})
        assert r.status_code == 201
    r = client.get("/api/v1/preferences/excluded-domains")
    items = r.json()["items"]
    assert [i["domain"] for i in items] == ["gamma.com", "beta.com", "alpha.com"]


# ─────────────────────────── autopilot ───────────────────────────


def test_enable_autopilot_paid_user(client_factory, paid_user) -> None:
    client = client_factory(paid_user)
    r = client.post("/api/v1/autopilot/enable")
    assert r.status_code == 200
    assert r.json()["autopilot_enabled"] is True


def test_enable_autopilot_free_user_403(client_factory, free_user) -> None:
    client = client_factory(free_user)
    r = client.post("/api/v1/autopilot/enable")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "paid_required"


def test_disable_autopilot(client_factory, paid_user, db: Session) -> None:
    paid_user.autopilot_enabled = True
    db.add(paid_user)
    db.commit()

    client = client_factory(paid_user)
    r = client.post("/api/v1/autopilot/disable")
    assert r.status_code == 200
    body = r.json()
    assert body["autopilot_enabled"] is False
    assert body["autopilot_paused_at"] is None


def test_pause_autopilot_sets_paused_at(client_factory, paid_user) -> None:
    client = client_factory(paid_user)
    client.post("/api/v1/autopilot/enable")
    r = client.post("/api/v1/autopilot/pause")
    assert r.status_code == 200
    assert r.json()["autopilot_paused_at"] is not None


def test_resume_autopilot_clears_paused_at(client_factory, paid_user) -> None:
    client = client_factory(paid_user)
    client.post("/api/v1/autopilot/enable")
    client.post("/api/v1/autopilot/pause")
    r = client.post("/api/v1/autopilot/resume")
    assert r.status_code == 200
    assert r.json()["autopilot_paused_at"] is None


def test_resume_clears_auto_pause_history(client_factory, paid_user) -> None:
    """A user can re-enable autopilot after pause and the state is clean."""
    client = client_factory(paid_user)
    client.post("/api/v1/autopilot/enable")
    client.post("/api/v1/autopilot/pause")
    # Re-enabling clears paused_at (see service.enable_autopilot).
    r = client.post("/api/v1/autopilot/enable")
    assert r.status_code == 200
    body = r.json()
    assert body["autopilot_enabled"] is True
    assert body["autopilot_paused_at"] is None


# ─────────────────────────── stop conditions ───────────────────────────


def test_enable_autopilot_sets_enabled_at(client_factory, paid_user, db) -> None:
    """Every toggle-on stamps autopilot_enabled_at so counters reset."""
    client = client_factory(paid_user)
    r = client.post("/api/v1/autopilot/enable")
    assert r.status_code == 200
    assert r.json()["autopilot_enabled_at"] is not None


def test_disable_autopilot_preserves_enabled_at(client_factory, paid_user, db) -> None:
    """Toggle-off keeps enabled_at for the audit trail."""
    client = client_factory(paid_user)
    client.post("/api/v1/autopilot/enable")
    before = client.get("/api/v1/preferences").json()["autopilot_enabled_at"]
    assert before is not None

    r = client.post("/api/v1/autopilot/disable")
    assert r.json()["autopilot_enabled_at"] == before


def test_pause_stamps_reason_user(client_factory, paid_user) -> None:
    client = client_factory(paid_user)
    client.post("/api/v1/autopilot/enable")
    client.post("/api/v1/autopilot/pause")
    r = client.get("/api/v1/preferences")
    assert r.json()["autopilot_paused_reason"] == "user"


def test_resume_clears_paused_reason(client_factory, paid_user) -> None:
    client = client_factory(paid_user)
    client.post("/api/v1/autopilot/enable")
    client.post("/api/v1/autopilot/pause")
    client.post("/api/v1/autopilot/resume")
    r = client.get("/api/v1/preferences")
    assert r.json()["autopilot_paused_reason"] is None


def test_patch_stop_type_replies_with_value(client_factory, free_user) -> None:
    client = client_factory(free_user)
    r = client.patch(
        "/api/v1/preferences",
        json={"autopilot_stop_type": "replies", "autopilot_stop_at_replies": 3},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["autopilot_stop_type"] == "replies"
    assert body["autopilot_stop_at_replies"] == 3
    assert body["autopilot_stop_at_date"] is None
    assert body["autopilot_stop_at_budget"] is None


def test_patch_stop_type_end_date_with_value(client_factory, free_user) -> None:
    target = (utcnow().date() + timedelta(days=14)).isoformat()
    client = client_factory(free_user)
    r = client.patch(
        "/api/v1/preferences",
        json={"autopilot_stop_type": "end_date", "autopilot_stop_at_date": target},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["autopilot_stop_type"] == "end_date"
    assert body["autopilot_stop_at_date"] == target


def test_patch_stop_type_budget_with_value(client_factory, free_user) -> None:
    client = client_factory(free_user)
    r = client.patch(
        "/api/v1/preferences",
        json={"autopilot_stop_type": "budget", "autopilot_stop_at_budget": 50},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["autopilot_stop_type"] == "budget"
    assert body["autopilot_stop_at_budget"] == 50


def test_switching_stop_type_clears_previous_value(client_factory, free_user) -> None:
    """The critical exclusivity check: set replies=3, then switch to budget=50.
    stop_at_replies must be nulled out server-side."""
    client = client_factory(free_user)
    client.patch(
        "/api/v1/preferences",
        json={"autopilot_stop_type": "replies", "autopilot_stop_at_replies": 3},
    )
    r = client.patch(
        "/api/v1/preferences",
        json={"autopilot_stop_type": "budget", "autopilot_stop_at_budget": 50},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["autopilot_stop_type"] == "budget"
    assert body["autopilot_stop_at_budget"] == 50
    assert body["autopilot_stop_at_replies"] is None
    assert body["autopilot_stop_at_date"] is None


def test_switching_to_none_clears_all_value_columns(client_factory, free_user) -> None:
    client = client_factory(free_user)
    client.patch(
        "/api/v1/preferences",
        json={"autopilot_stop_type": "budget", "autopilot_stop_at_budget": 100},
    )
    r = client.patch("/api/v1/preferences", json={"autopilot_stop_type": "none"})
    body = r.json()
    assert body["autopilot_stop_type"] == "none"
    assert body["autopilot_stop_at_replies"] is None
    assert body["autopilot_stop_at_date"] is None
    assert body["autopilot_stop_at_budget"] is None


def test_patch_invalid_stop_type_rejected(client_factory, free_user) -> None:
    client = client_factory(free_user)
    r = client.patch("/api/v1/preferences", json={"autopilot_stop_type": "bogus"})
    assert r.status_code == 422


def test_patch_invalid_replies_value_rejected(client_factory, free_user) -> None:
    """Only 1, 3, 5 are legal reply thresholds."""
    client = client_factory(free_user)
    r = client.patch("/api/v1/preferences", json={"autopilot_stop_at_replies": 2})
    assert r.status_code == 422


def test_patch_invalid_budget_value_rejected(client_factory, free_user) -> None:
    """Only 25, 50, 100, 200 are legal budgets."""
    client = client_factory(free_user)
    r = client.patch("/api/v1/preferences", json={"autopilot_stop_at_budget": 42})
    assert r.status_code == 422


def test_patch_past_end_date_rejected(client_factory, free_user) -> None:
    yesterday = (utcnow().date() - timedelta(days=1)).isoformat()
    client = client_factory(free_user)
    r = client.patch("/api/v1/preferences", json={"autopilot_stop_at_date": yesterday})
    assert r.status_code == 422


def test_patch_end_date_beyond_90_days_rejected(client_factory, free_user) -> None:
    far = (utcnow().date() + timedelta(days=100)).isoformat()
    client = client_factory(free_user)
    r = client.patch("/api/v1/preferences", json={"autopilot_stop_at_date": far})
    assert r.status_code == 422
