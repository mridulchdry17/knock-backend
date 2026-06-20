"""Unit tests for `app.services.refresh_tokens` — the rotation + reuse-detection core.

We exercise the service against a real DB session (the in-memory test fixture)
so the SQL `revoke_family` UPDATE actually runs. The HTTP boundary is tested
separately in test_refresh_tokens_router.py.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models import RefreshToken
from app.services import refresh_tokens as rt
from tests.conftest import _make_user


def test_issue_starts_a_fresh_family_when_family_id_omitted(db: Session) -> None:
    user = _make_user(db, email="a@x.com")
    a = rt.issue(db, user_id=user.id)
    b = rt.issue(db, user_id=user.id)
    db.commit()
    # Two separate logins → two separate families.
    assert a.family_id != b.family_id


def test_validate_and_rotate_happy_path(db: Session) -> None:
    user = _make_user(db, email="a@x.com")
    a = rt.issue(db, user_id=user.id)
    db.commit()

    result = rt.validate_and_rotate(db, raw_token=a.raw_token)
    db.commit()

    assert result.rotated is not None
    assert result.reuse_detected is False
    assert result.invalid is False
    assert result.user_id == user.id
    # Same family, new token.
    assert result.rotated.family_id == a.family_id
    assert result.rotated.raw_token != a.raw_token

    # The old row is revoked + linked to the new row.
    old = db.get(RefreshToken, a.raw_token)
    assert old is not None
    assert old.revoked_at is not None
    assert old.replaced_by_id == result.rotated.raw_token


def test_validate_unknown_token_is_invalid_not_reuse(db: Session) -> None:
    result = rt.validate_and_rotate(db, raw_token="garbage-token-not-in-db")
    assert result.invalid is True
    assert result.reuse_detected is False
    assert result.rotated is None


def test_validate_expired_token_is_invalid(db: Session) -> None:
    user = _make_user(db, email="a@x.com")
    a = rt.issue(db, user_id=user.id)
    # Backdate it past TTL.
    row = db.get(RefreshToken, a.raw_token)
    assert row is not None
    row.expires_at = utcnow() - timedelta(days=1)
    db.add(row)
    db.commit()

    result = rt.validate_and_rotate(db, raw_token=a.raw_token)
    assert result.invalid is True
    assert result.reuse_detected is False


def test_validate_revoked_token_is_invalid(db: Session) -> None:
    """A token revoked via logout (revoked_at set, no replaced_by_id) must
    be 'invalid' — NOT reuse-detected. We mustn't burn the family on a
    cookie that's just stale from a prior logout."""
    user = _make_user(db, email="a@x.com")
    a = rt.issue(db, user_id=user.id)
    row = db.get(RefreshToken, a.raw_token)
    assert row is not None
    row.revoked_at = utcnow()  # logout path: no successor
    db.add(row)
    db.commit()

    result = rt.validate_and_rotate(db, raw_token=a.raw_token)
    assert result.invalid is True
    assert result.reuse_detected is False


def test_reuse_detection_revokes_whole_family(db: Session) -> None:
    """Present a token that has been rotated → revoke the family. The new
    token (rotated successor) is also nuked, so the legit device gets kicked
    out on its next refresh too. That's the trade-off; better than letting
    an attacker who replayed the old token stay logged in."""
    user = _make_user(db, email="a@x.com")
    a = rt.issue(db, user_id=user.id)
    db.commit()

    # First rotation — legitimate.
    r1 = rt.validate_and_rotate(db, raw_token=a.raw_token)
    db.commit()
    assert r1.rotated is not None
    new_token = r1.rotated.raw_token
    family_id = a.family_id

    # Attacker presents the OLD token (replayed copy) — that token's
    # replaced_by_id is now set, so this is reuse.
    r2 = rt.validate_and_rotate(db, raw_token=a.raw_token)
    db.commit()
    assert r2.reuse_detected is True
    assert r2.rotated is None
    assert r2.user_id == user.id

    # The legitimate successor is now revoked too — whole family burned.
    successor = db.get(RefreshToken, new_token)
    assert successor is not None
    assert successor.revoked_at is not None

    # Legit device's NEXT refresh attempt (using the successor) is invalid,
    # NOT reuse-detected (successor wasn't already-replaced — it was just
    # nuked by family revocation).
    r3 = rt.validate_and_rotate(db, raw_token=new_token)
    assert r3.invalid is True
    assert r3.reuse_detected is False

    # And the family_id sanity-check: the family is the same one as `a`.
    assert successor.family_id == family_id


def test_revoke_family_for_token_idempotent(db: Session) -> None:
    user = _make_user(db, email="a@x.com")
    a = rt.issue(db, user_id=user.id)
    b = rt.issue(db, user_id=user.id, family_id=a.family_id)
    db.commit()

    first = rt.revoke_family_for_token(db, raw_token=a.raw_token)
    db.commit()
    assert first == 2  # both rows revoked

    # Second call: already-revoked rows are NOT double-stamped → returns 0.
    second = rt.revoke_family_for_token(db, raw_token=a.raw_token)
    db.commit()
    assert second == 0

    # Both rows are revoked, regardless of which one we passed.
    assert db.get(RefreshToken, a.raw_token).revoked_at is not None  # type: ignore[union-attr]
    assert db.get(RefreshToken, b.raw_token).revoked_at is not None  # type: ignore[union-attr]


def test_revoke_family_for_unknown_token_returns_zero(db: Session) -> None:
    assert rt.revoke_family_for_token(db, raw_token="nope") == 0
