"""Tests for the OAuth tier-decision tree and onboarding service.

Covers the matrix from project_soft_gate_and_approval.md:

  - super_admin email → super_admin / dashboard
  - waitlist match (new user) → free / dashboard, waitlist_email set
  - returning approved user → keep tier, dashboard
  - returning pending user → stays pending, onboarding
  - new user no waitlist match → pending / onboarding
  - claim_waitlist OK / NOT_FOUND / TAKEN
  - join_waitlist idempotent

The OAuth Google identity layer is NOT tested here — that requires mocking
the google-auth-oauthlib stack, which is its own concern. We test the
business-logic seam (`decide_tier_and_destination`) with real DB rows.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.repositories import waitlist as waitlist_repo
from app.services import onboarding as onboarding_service
from app.services.auth import decide_tier_and_destination
from tests.conftest import _make_user

# ─────────────────────────── decision tree ───────────────────────────


def test_super_admin_email_promotes(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.auth.settings.SUPER_ADMIN_EMAILS",
        "admin@knock.app",
    )
    user = _make_user(db, email="admin@knock.app", tier="pending")

    decision = decide_tier_and_destination(db, user)

    assert decision.new_tier == "super_admin"
    assert decision.next_path == "/today"
    assert decision.claim_email is None


def test_super_admin_check_is_case_insensitive(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.auth.settings.SUPER_ADMIN_EMAILS",
        "admin@knock.app",
    )
    user = _make_user(db, email="ADMIN@KNOCK.APP", tier="pending")

    decision = decide_tier_and_destination(db, user)
    assert decision.new_tier == "super_admin"


def test_approved_waitlist_match_auto_claims_to_free(
    db: Session, approved_waitlist_email: str
) -> None:
    user = _make_user(db, email=approved_waitlist_email, tier="pending")

    decision = decide_tier_and_destination(db, user)

    assert decision.new_tier == "free"
    assert decision.next_path == "/today"
    assert decision.claim_email == approved_waitlist_email


def test_unapproved_waitlist_match_stays_pending_but_links(
    db: Session, waitlist_email: str
) -> None:
    """On the list but not allowed in yet → still pending, but we remember the
    match (claim_email) so the admin can find + approve them."""
    user = _make_user(db, email=waitlist_email, tier="pending")

    decision = decide_tier_and_destination(db, user)

    assert decision.new_tier == "pending"
    assert decision.next_path == "/awaiting-approval"
    assert decision.claim_email == waitlist_email


def test_no_waitlist_match_stays_pending(db: Session) -> None:
    user = _make_user(db, email="random@stranger.com", tier="pending")

    decision = decide_tier_and_destination(db, user)

    assert decision.new_tier == "pending"
    assert decision.next_path == "/onboarding"
    assert decision.claim_email is None


def test_returning_approved_user_keeps_tier(db: Session) -> None:
    user = _make_user(
        db, email="user@example.com", tier="free", waitlist_email="user@example.com"
    )

    decision = decide_tier_and_destination(db, user)

    assert decision.new_tier == "free"
    assert decision.next_path == "/today"
    assert decision.claim_email is None


def test_returning_pending_user_still_pending(db: Session) -> None:
    """User joined waitlist but isn't yet approved by super_admin."""
    user = _make_user(
        db, email="hopeful@example.com", tier="pending", waitlist_email="hopeful@example.com"
    )

    decision = decide_tier_and_destination(db, user)

    assert decision.new_tier == "pending"
    assert decision.next_path == "/awaiting-approval"


def test_paid_returning_user_keeps_paid(db: Session) -> None:
    user = _make_user(
        db, email="customer@example.com", tier="paid", waitlist_email="customer@example.com"
    )

    decision = decide_tier_and_destination(db, user)

    assert decision.new_tier == "paid"
    assert decision.next_path == "/today"


# ─────────────────────────── claim_waitlist ───────────────────────────


def test_claim_approved_waitlist_ok(db: Session, approved_waitlist_email: str) -> None:
    user = _make_user(db, email="other@example.com", tier="pending")

    result = onboarding_service.claim_waitlist(db, user, approved_waitlist_email)

    assert result is onboarding_service.ClaimResult.OK
    db.refresh(user)
    assert user.waitlist_email == approved_waitlist_email
    assert user.tier == "free"


def test_claim_unapproved_waitlist_links_but_stays_pending(
    db: Session, waitlist_email: str
) -> None:
    """Claiming an un-approved entry links the spot (so the admin can find them)
    but does NOT grant access — they wait for approval."""
    user = _make_user(db, email="other@example.com", tier="pending")

    result = onboarding_service.claim_waitlist(db, user, waitlist_email)

    assert result is onboarding_service.ClaimResult.PENDING_APPROVAL
    db.refresh(user)
    assert user.waitlist_email == waitlist_email  # linked
    assert user.tier == "pending"  # but gated


def test_claim_waitlist_not_found(db: Session) -> None:
    user = _make_user(db, email="other@example.com", tier="pending")

    result = onboarding_service.claim_waitlist(db, user, "ghost@nowhere.com")

    assert result is onboarding_service.ClaimResult.NOT_FOUND
    db.refresh(user)
    assert user.waitlist_email is None
    assert user.tier == "pending"


def test_claim_waitlist_taken_by_other_user(db: Session, waitlist_email: str) -> None:
    other = _make_user(
        db,
        email="first@example.com",
        google_sub="g-1",
        tier="free",
        waitlist_email=waitlist_email,
    )
    user = _make_user(db, email="latecomer@example.com", google_sub="g-2", tier="pending")

    result = onboarding_service.claim_waitlist(db, user, waitlist_email)

    assert result is onboarding_service.ClaimResult.TAKEN
    db.refresh(user)
    assert user.waitlist_email is None
    assert user.tier == "pending"
    db.refresh(other)
    assert other.waitlist_email == waitlist_email  # unchanged


def test_claim_waitlist_idempotent_for_same_user(
    db: Session, approved_waitlist_email: str
) -> None:
    user = _make_user(db, email="other@example.com", tier="pending")

    first = onboarding_service.claim_waitlist(db, user, approved_waitlist_email)
    second = onboarding_service.claim_waitlist(db, user, approved_waitlist_email)

    assert first is onboarding_service.ClaimResult.OK
    assert second is onboarding_service.ClaimResult.OK


def test_claim_waitlist_normalizes_email_case(
    db: Session, approved_waitlist_email: str
) -> None:
    user = _make_user(db, email="other@example.com", tier="pending")

    result = onboarding_service.claim_waitlist(db, user, approved_waitlist_email.upper())

    assert result is onboarding_service.ClaimResult.OK
    db.refresh(user)
    assert user.waitlist_email == approved_waitlist_email  # stored lowercased


# ─────────────────────────── join_waitlist ───────────────────────────


def test_join_waitlist_adds_row_and_sets_pending(db: Session) -> None:
    user = _make_user(db, email="newcomer@example.com", tier="pending")

    onboarding_service.join_waitlist(db, user)

    db.refresh(user)
    assert user.waitlist_email == "newcomer@example.com"
    assert user.tier == "pending"
    assert waitlist_repo.exists(db, "newcomer@example.com")


def test_join_waitlist_idempotent(db: Session) -> None:
    user = _make_user(db, email="newcomer@example.com", tier="pending")

    onboarding_service.join_waitlist(db, user)
    onboarding_service.join_waitlist(db, user)  # second call must not blow up

    db.refresh(user)
    assert user.waitlist_email == "newcomer@example.com"


def test_join_waitlist_when_email_already_publicly_signed_up(db: Session) -> None:
    """User submitted to public /api/v1/waitlist before OAuth'ing in."""
    waitlist_repo.add(db, "newcomer@example.com")
    db.commit()
    user = _make_user(db, email="newcomer@example.com", tier="pending")

    onboarding_service.join_waitlist(db, user)

    db.refresh(user)
    assert user.waitlist_email == "newcomer@example.com"
    assert user.tier == "pending"
