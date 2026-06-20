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


def test_network_retry_within_grace_returns_successor_not_reuse(db: Session) -> None:
    """A token presented again RIGHT AFTER it's been rotated is treated as a
    network-retry race (the legitimate client's first refresh response was
    lost in flight). Service returns the existing successor — DOES NOT burn
    the family. This is the v1 fix for the network-blip false-positive."""
    user = _make_user(db, email="a@x.com")
    a = rt.issue(db, user_id=user.id)
    db.commit()

    r1 = rt.validate_and_rotate(db, raw_token=a.raw_token)
    db.commit()
    assert r1.rotated is not None
    successor_token = r1.rotated.raw_token

    # Immediate replay (within grace) — must NOT trigger reuse.
    r2 = rt.validate_and_rotate(db, raw_token=a.raw_token)
    db.commit()
    assert r2.reuse_detected is False
    assert r2.invalid is False
    # And it hands back the same successor (idempotent rotation).
    assert r2.rotated is not None
    assert r2.rotated.raw_token == successor_token

    # Successor row is still active — family not burned.
    successor = db.get(RefreshToken, successor_token)
    assert successor is not None
    assert successor.revoked_at is None


def test_reuse_detection_outside_grace_revokes_family(db: Session) -> None:
    """Beyond the 30s grace window, a replay of a rotated token IS reuse —
    burn the whole family (legitimate device + attacker both kicked out)."""
    user = _make_user(db, email="a@x.com")
    a = rt.issue(db, user_id=user.id)
    db.commit()

    r1 = rt.validate_and_rotate(db, raw_token=a.raw_token)
    db.commit()
    assert r1.rotated is not None
    successor_token = r1.rotated.raw_token
    family_id = a.family_id

    # Backdate the rotation past the grace window — simulates a stolen
    # cookie replayed minutes/hours after the legitimate rotation.
    old_row = db.get(RefreshToken, a.raw_token)
    assert old_row is not None
    old_row.revoked_at = utcnow() - timedelta(minutes=5)
    db.add(old_row)
    db.commit()

    r2 = rt.validate_and_rotate(db, raw_token=a.raw_token)
    db.commit()
    assert r2.reuse_detected is True
    assert r2.rotated is None
    assert r2.user_id == user.id

    # The legitimate successor is now revoked — whole family burned.
    successor = db.get(RefreshToken, successor_token)
    assert successor is not None
    assert successor.revoked_at is not None
    assert successor.family_id == family_id

    # Legit device's NEXT refresh attempt (using the successor) is invalid,
    # NOT reuse-detected.
    r3 = rt.validate_and_rotate(db, raw_token=successor_token)
    assert r3.invalid is True
    assert r3.reuse_detected is False


def test_atomic_rotation_under_simulated_race(db: Session) -> None:
    """Simulate two concurrent rotations that both started with the same
    pre-claim view of the row. Only ONE can win the atomic claim_for_rotation
    UPDATE; the loser must NOT mint an orphan successor — it must fall
    through to the network-retry-grace path and return the winner's
    successor.

    Construction: we hand-fire a `claim_for_rotation` to commit a race
    state, then call validate_and_rotate. The service must NOT see this as
    reuse (within grace) and must return the existing successor."""
    user = _make_user(db, email="a@x.com")
    a = rt.issue(db, user_id=user.id)
    db.commit()

    # Simulate the "winner" rotation: claim atomically + insert successor.
    from app.repositories import refresh_tokens as repo

    winner_token_id = "winner-successor-token"
    assert repo.claim_for_rotation(
        db, raw_token=a.raw_token, new_token_id=winner_token_id
    )
    winner_row = RefreshToken(
        id=winner_token_id,
        user_id=user.id,
        family_id=a.family_id,
        expires_at=utcnow() + timedelta(days=30),
    )
    db.add(winner_row)
    db.commit()

    # Now a "loser" comes in with the same old raw_token. Service must
    # detect via claim_for_rotation returning False, fall through, and
    # return the winner's successor (within grace).
    loser_result = rt.validate_and_rotate(db, raw_token=a.raw_token)
    assert loser_result.rotated is not None
    assert loser_result.rotated.raw_token == winner_token_id
    assert loser_result.reuse_detected is False
    assert loser_result.invalid is False


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
