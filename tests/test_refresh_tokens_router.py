"""HTTP tests for POST /api/v1/auth/refresh + the augmented /logout & /disconnect.

Exercises the real router + dependency stack. The refresh endpoint is the
critical security boundary — these tests pin the wire contract:

  - missing cookie → 401 with code='no_refresh_token'
  - invalid cookie (expired / revoked-via-logout) → 401 with code='refresh_invalid'
  - REUSE → 401 with code='refresh_reuse_detected', whole family revoked
  - happy path → 200 with {access_token: "..."} AND a fresh Set-Cookie header

The logout / disconnect tests pin that the cookie is cleared and the family
revoked on the way out.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from datetime import timedelta

from app.core.cookies import REFRESH_TOKEN_COOKIE
from app.core.time import utcnow
from app.db.session import get_db
from app.main import app
from app.models import RefreshToken
from app.services import refresh_tokens as rt_service
from tests.conftest import _make_user


@pytest.fixture
def client_factory(engine: Engine):
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _make() -> TestClient:
        def _override_get_db():
            s = factory()
            try:
                yield s
            finally:
                s.close()

        app.dependency_overrides[get_db] = _override_get_db
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def _seed_token(db: Session, *, email: str = "u@x.com") -> tuple[int, str, str]:
    """Helper: create a user + refresh token. Returns (user_id, family_id, raw_token)."""
    user = _make_user(db, email=email)
    issued = rt_service.issue(db, user_id=user.id)
    db.commit()
    return user.id, issued.family_id, issued.raw_token


# ─────────────────────────── /refresh — failure modes ───────────────────────────


def test_refresh_no_cookie_returns_401(client_factory) -> None:
    client = client_factory()
    r = client.post("/api/v1/auth/refresh")
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "no_refresh_token"


def test_refresh_unknown_token_returns_401_invalid(db: Session, client_factory) -> None:
    client = client_factory()
    r = client.post(
        "/api/v1/auth/refresh",
        cookies={REFRESH_TOKEN_COOKIE: "no-such-token-in-db"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "refresh_invalid"


def test_refresh_revoked_token_is_invalid_not_reuse(
    db: Session, client_factory
) -> None:
    """A token revoked via logout (no successor) → 'refresh_invalid', NOT
    'refresh_reuse_detected'. We mustn't burn the family on a stale cookie
    from a clean logout."""
    user_id, family_id, raw = _seed_token(db)
    # Simulate logout-style revoke: revoked_at set, replaced_by_id NOT set.
    rt_service.revoke_family_for_token(db, raw_token=raw)
    db.commit()

    client = client_factory()
    r = client.post(
        "/api/v1/auth/refresh", cookies={REFRESH_TOKEN_COOKIE: raw}
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "refresh_invalid"


def test_refresh_network_retry_within_grace_returns_existing_successor(
    db: Session, client_factory
) -> None:
    """v1 fix for the legitimate-client network-blip case: presenting the
    same old token within the 30s grace window (i.e. immediately after a
    rotation) MUST NOT trigger reuse — it should return the existing
    successor instead, and the family stays intact."""
    user_id, family_id, raw_old = _seed_token(db)

    client = client_factory()
    first = client.post(
        "/api/v1/auth/refresh", cookies={REFRESH_TOKEN_COOKIE: raw_old}
    )
    assert first.status_code == 200
    successor = first.cookies.get(REFRESH_TOKEN_COOKIE)
    assert successor is not None

    # Immediate replay of the old token — within grace → 200 + same successor.
    second = client.post(
        "/api/v1/auth/refresh", cookies={REFRESH_TOKEN_COOKIE: raw_old}
    )
    assert second.status_code == 200
    assert second.cookies.get(REFRESH_TOKEN_COOKIE) == successor

    # Successor row is still active — family not burned.
    db.expire_all()
    successor_row = db.get(RefreshToken, successor)
    assert successor_row is not None
    assert successor_row.revoked_at is None


def test_refresh_reuse_outside_grace_revokes_family(
    db: Session, client_factory
) -> None:
    """Beyond the grace window, a replay of a rotated token IS reuse — burn
    the whole family. Verifies the security boundary still holds."""
    user_id, family_id, raw_old = _seed_token(db)

    client = client_factory()
    first = client.post(
        "/api/v1/auth/refresh", cookies={REFRESH_TOKEN_COOKIE: raw_old}
    )
    assert first.status_code == 200
    successor = first.cookies.get(REFRESH_TOKEN_COOKIE)
    assert successor is not None

    # Backdate the rotation past the grace window — simulates a stolen
    # cookie being replayed minutes later.
    old_row = db.get(RefreshToken, raw_old)
    assert old_row is not None
    old_row.revoked_at = utcnow() - timedelta(minutes=5)
    db.add(old_row)
    db.commit()

    second = client.post(
        "/api/v1/auth/refresh", cookies={REFRESH_TOKEN_COOKIE: raw_old}
    )
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "refresh_reuse_detected"

    # Successor is revoked — whole family is dead.
    db.expire_all()
    successor_row = db.get(RefreshToken, successor)
    assert successor_row is not None
    assert successor_row.revoked_at is not None

    # And the response clears the cookie so the dead value isn't replayed.
    set_cookie_headers = [v for k, v in second.headers.items() if k.lower() == "set-cookie"]
    cleared = next(
        (h for h in set_cookie_headers if h.startswith(f"{REFRESH_TOKEN_COOKIE}=")),
        None,
    )
    assert cleared is not None
    assert "Max-Age=0" in cleared or "expires=Thu, 01 Jan 1970" in cleared.lower()


# ─────────────────────────── /refresh — happy path ───────────────────────────


def test_refresh_happy_path_returns_access_token_and_rotates_cookie(
    db: Session, client_factory
) -> None:
    user_id, family_id, raw_old = _seed_token(db)
    client = client_factory()

    r = client.post(
        "/api/v1/auth/refresh", cookies={REFRESH_TOKEN_COOKIE: raw_old}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "access_token" in body
    assert isinstance(body["access_token"], str)
    assert len(body["access_token"]) >= 32  # 32 bytes → 43-char urlsafe

    # New refresh cookie set on the response (different from the one we sent).
    new_cookie = r.cookies.get(REFRESH_TOKEN_COOKIE)
    assert new_cookie is not None
    assert new_cookie != raw_old

    # The Set-Cookie header carries HttpOnly + SameSite=lax so JS can't
    # exfiltrate via document.cookie.
    set_cookie_headers = [v for k, v in r.headers.items() if k.lower() == "set-cookie"]
    refresh_cookie_header = next(
        (h for h in set_cookie_headers if h.startswith(f"{REFRESH_TOKEN_COOKIE}=")),
        None,
    )
    assert refresh_cookie_header is not None
    assert "HttpOnly" in refresh_cookie_header
    assert "samesite=lax" in refresh_cookie_header.lower()

    # Old row is revoked + linked to the new one.
    db.expire_all()
    old_row = db.get(RefreshToken, raw_old)
    assert old_row is not None
    assert old_row.revoked_at is not None
    assert old_row.replaced_by_id == new_cookie


def test_refresh_rotation_keeps_same_family(db: Session, client_factory) -> None:
    user_id, original_family, raw = _seed_token(db)
    client = client_factory()

    r = client.post(
        "/api/v1/auth/refresh", cookies={REFRESH_TOKEN_COOKIE: raw}
    )
    new_token = r.cookies.get(REFRESH_TOKEN_COOKIE)
    assert new_token is not None

    db.expire_all()
    new_row = db.get(RefreshToken, new_token)
    assert new_row is not None
    assert new_row.family_id == original_family


# ─────────────────────────── /logout ───────────────────────────


def test_logout_clears_cookie_and_revokes_family(
    db: Session, client_factory
) -> None:
    from app.core.deps import get_current_user
    from app.models import Session as SessionRow
    from app.services import sessions as sessions_service

    user_id, family_id, refresh_raw = _seed_token(db)
    user = db.query(_make_user.__globals__["User"]).filter_by(id=user_id).one()

    # Issue an access token + override the bearer dep to return our user.
    access_session = sessions_service.issue(db, user_id=user.id)
    access_token = access_session.id  # capture before expire_all() invalidates the row
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = client_factory()
        r = client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {access_token}"},
            cookies={REFRESH_TOKEN_COOKIE: refresh_raw},
        )
        assert r.status_code == 200

        # Access token (sessions row) is deleted.
        db.expire_all()
        assert db.get(SessionRow, access_token) is None

        # Refresh family is revoked.
        refresh_row = db.get(RefreshToken, refresh_raw)
        assert refresh_row is not None
        assert refresh_row.revoked_at is not None

        # Cookie cleared on response — Max-Age=0 or expires in the past.
        set_cookie_headers = [v for k, v in r.headers.items() if k.lower() == "set-cookie"]
        cleared = next(
            (h for h in set_cookie_headers if h.startswith(f"{REFRESH_TOKEN_COOKIE}=")),
            None,
        )
        assert cleared is not None
        # FastAPI's delete_cookie writes Max-Age=0; some versions write an
        # epoch-zero Expires. Accept either signal.
        assert "Max-Age=0" in cleared or "expires=Thu, 01 Jan 1970" in cleared.lower()
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_logout_without_refresh_cookie_still_succeeds(
    db: Session, client_factory
) -> None:
    """Logout must be idempotent — a client that already lost the cookie
    (or never had one) should still get a 200 and have its access-token
    row deleted."""
    from app.core.deps import get_current_user
    from app.services import sessions as sessions_service

    user = _make_user(db, email="u@x.com")
    access_session = sessions_service.issue(db, user_id=user.id)
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = client_factory()
        r = client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {access_session.id}"},
        )
        assert r.status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ─────────────────────────── /disconnect ───────────────────────────


def test_disconnect_revokes_every_session_and_refresh_token_for_user(
    db: Session, client_factory
) -> None:
    """/disconnect is the 'sign-out-everywhere' button — it must wipe every
    access-token session AND every refresh token (across all families /
    devices) for the user."""
    from app.core.deps import get_current_user
    from app.models import Session as SessionRow
    from app.services import sessions as sessions_service

    user = _make_user(db, email="u@x.com")

    # Two separate "logins" → two refresh families, plus two access tokens.
    fam_a = rt_service.issue(db, user_id=user.id)
    fam_b = rt_service.issue(db, user_id=user.id)
    sess_a = sessions_service.issue(db, user_id=user.id)
    sess_b = sessions_service.issue(db, user_id=user.id)
    sess_a_id, sess_b_id = sess_a.id, sess_b.id
    db.commit()

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = client_factory()
        r = client.post(
            "/api/v1/auth/disconnect",
            headers={"Authorization": f"Bearer {sess_a_id}"},
            cookies={REFRESH_TOKEN_COOKIE: fam_a.raw_token},
        )
        assert r.status_code == 200

        db.expire_all()
        # Every access-token session for this user is gone.
        assert db.get(SessionRow, sess_a_id) is None
        assert db.get(SessionRow, sess_b_id) is None
        # Every refresh token for this user is revoked (including the OTHER
        # device's family that didn't send a cookie on this request).
        for raw in (fam_a.raw_token, fam_b.raw_token):
            row = db.get(RefreshToken, raw)
            assert row is not None
            assert row.revoked_at is not None
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_disconnect_clears_refresh_cookie(db: Session, client_factory) -> None:
    """The current device's HttpOnly refresh cookie must be cleared on the
    response so the browser stops replaying a now-orphaned secret."""
    from app.core.deps import get_current_user
    from app.services import sessions as sessions_service

    user = _make_user(db, email="u@x.com")
    fam = rt_service.issue(db, user_id=user.id)
    sess = sessions_service.issue(db, user_id=user.id)
    sess_id = sess.id
    db.commit()

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = client_factory()
        r = client.post(
            "/api/v1/auth/disconnect",
            headers={"Authorization": f"Bearer {sess_id}"},
            cookies={REFRESH_TOKEN_COOKIE: fam.raw_token},
        )
        assert r.status_code == 200
        set_cookie_headers = [v for k, v in r.headers.items() if k.lower() == "set-cookie"]
        cleared = next(
            (h for h in set_cookie_headers if h.startswith(f"{REFRESH_TOKEN_COOKIE}=")),
            None,
        )
        assert cleared is not None
        assert "Max-Age=0" in cleared or "expires=Thu, 01 Jan 1970" in cleared.lower()
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_disconnect_is_idempotent(db: Session, client_factory) -> None:
    """Calling /disconnect twice in a row succeeds both times. The second
    call has nothing to revoke (everything is already wiped) and still
    returns 200."""
    from app.core.deps import get_current_user
    from app.services import sessions as sessions_service

    user = _make_user(db, email="u@x.com")
    sess1 = sessions_service.issue(db, user_id=user.id)
    sess1_id = sess1.id
    db.commit()

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = client_factory()
        r1 = client.post(
            "/api/v1/auth/disconnect",
            headers={"Authorization": f"Bearer {sess1_id}"},
        )
        assert r1.status_code == 200

        # Issue a fresh access token for the second call (the first call
        # nuked all the previous ones).
        sess2 = sessions_service.issue(db, user_id=user.id)
        sess2_id = sess2.id
        db.commit()
        r2 = client.post(
            "/api/v1/auth/disconnect",
            headers={"Authorization": f"Bearer {sess2_id}"},
        )
        assert r2.status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)
