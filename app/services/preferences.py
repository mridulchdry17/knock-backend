"""Preferences orchestration.

Wraps the repository with domain rules:

  - Excluded-domain format validation + normalization (strip leading '@',
    lowercase).
  - Daily-summary toggle gating — only meaningful for users with autopilot
    enabled; we surface this as an explicit error rather than silently
    accepting a write the system will never act on.
  - Autopilot enable/disable/pause/resume semantics.

The router translates each return value into the right HTTP status.
"""
from __future__ import annotations

import re
from enum import StrEnum

from sqlalchemy.orm import Session as OrmSession

from app.core.time import utcnow
from app.models import User
from app.repositories import preferences as prefs_repo
from app.schemas.preferences import PreferencesOut, PreferencesPatch

# Relaxed-but-sane domain check. Accepts `acme.com`, `sub.acme.co.uk`,
# `@acme.com` (leading @ stripped before this match). Rejects spaces,
# IDN, and bare TLDs.
_DOMAIN_RE = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


class DomainResult(StrEnum):
    OK = "ok"
    INVALID = "invalid_domain"
    DUPLICATE = "duplicate"


def _normalize_domain(raw: str) -> str:
    """Strip whitespace, drop a leading '@', lowercase. Caller is responsible
    for format validation via `_DOMAIN_RE` afterwards."""
    return raw.strip().lstrip("@").lower()


# ─────────────────────────── reads ───────────────────────────


def build_preferences_out(user: User) -> PreferencesOut:
    return PreferencesOut(
        target_role=user.target_role,
        target_industries=prefs_repo.read_industries(user),
        target_location=user.target_location,
        notify_gmail_disconnect=user.notify_gmail_disconnect,
        notify_daily_summary=user.notify_daily_summary,
        autopilot_enabled=user.autopilot_enabled,
        autopilot_paused_at=user.autopilot_paused_at,
        autopilot_auto_pause_on_reply=user.autopilot_auto_pause_on_reply,
        resume_url=user.resume_url,
    )


# ─────────────────────────── writes ───────────────────────────


def update_preferences(
    db: OrmSession, user: User, patch: PreferencesPatch
) -> PreferencesOut:
    # `exclude_unset=True` is what gives us PATCH semantics: omitted fields
    # are not in the dict, so update_user_preferences leaves them alone.
    # Explicit `null` survives and clears the column.
    data = patch.model_dump(exclude_unset=True)
    prefs_repo.update_user_preferences(db, user, data)
    db.commit()
    db.refresh(user)
    return build_preferences_out(user)


def add_excluded_domain(db: OrmSession, user: User, domain: str) -> DomainResult:
    normalized = _normalize_domain(domain)
    if not _DOMAIN_RE.match(normalized):
        return DomainResult.INVALID
    _, was_created = prefs_repo.add_excluded_domain(db, user.id, normalized)
    if not was_created:
        return DomainResult.DUPLICATE
    db.commit()
    return DomainResult.OK


def remove_excluded_domain(db: OrmSession, user: User, domain: str) -> bool:
    normalized = _normalize_domain(domain)
    if not _DOMAIN_RE.match(normalized):
        # A malformed domain by definition isn't in the table; treat the same
        # as not-found so the router returns 404 rather than 422.
        return False
    was_deleted = prefs_repo.remove_excluded_domain(db, user.id, normalized)
    if was_deleted:
        db.commit()
    return was_deleted


# ─────────────────────────── autopilot ───────────────────────────


def enable_autopilot(db: OrmSession, user: User) -> None:
    user.autopilot_enabled = True
    user.autopilot_paused_at = None  # enabling clears any prior pause
    db.add(user)
    db.commit()


def disable_autopilot(db: OrmSession, user: User) -> None:
    user.autopilot_enabled = False
    # Leave autopilot_paused_at as-is — disable is not the same as resume.
    db.add(user)
    db.commit()


def pause_autopilot(db: OrmSession, user: User) -> None:
    user.autopilot_paused_at = utcnow()
    db.add(user)
    db.commit()


def resume_autopilot(db: OrmSession, user: User) -> None:
    user.autopilot_paused_at = None
    db.add(user)
    db.commit()
