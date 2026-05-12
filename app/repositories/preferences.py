"""Preferences repository — DB-level reads/writes for the user preferences
columns on `users` plus the `user_excluded_domains` table.

Service layer (app.services.preferences) owns validation; this module owns
SQL. Domain strings reaching these functions are assumed pre-normalized.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as OrmSession

from app.models import User, UserExcludedDomain

# ─────────────────────────── user-row preferences ───────────────────────────

# The exact set of `users` columns this module is allowed to mutate via
# `update_user_preferences`. Anything else in the patch dict is ignored —
# defensive layering so a bad caller can't accidentally write to `tier`.
_PATCHABLE_COLUMNS: frozenset[str] = frozenset(
    {
        "target_role",
        "target_location",
        "notify_gmail_disconnect",
        "notify_daily_summary",
        "autopilot_auto_pause_on_reply",
        "resume_url",
    }
)


def _industries_to_db(industries: list[str] | None) -> str | None:
    if industries is None:
        return None
    return json.dumps(industries)


def _industries_from_db(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(x) for x in decoded] if isinstance(decoded, list) else []


def read_industries(user: User) -> list[str]:
    """Decode the JSON `target_industries` blob for response serialization."""
    return _industries_from_db(user.target_industries)


def update_user_preferences(
    db: OrmSession, user: User, patch: dict[str, Any]
) -> None:
    """Apply a partial update. Only keys in `_PATCHABLE_COLUMNS` plus
    `target_industries` (special-cased for JSON serialization) are written.

    Caller is responsible for `db.commit()` so the surrounding service can
    bundle multiple mutations into one transaction.
    """
    for key, value in patch.items():
        if key == "target_industries":
            user.target_industries = _industries_to_db(value)
        elif key in _PATCHABLE_COLUMNS:
            setattr(user, key, value)
    db.add(user)


# ─────────────────────────── excluded domains ───────────────────────────


def list_excluded_domains(db: OrmSession, user_id: int) -> list[UserExcludedDomain]:
    """Newest first — UI presents the most recently added domain at the top."""
    return list(
        db.scalars(
            select(UserExcludedDomain)
            .where(UserExcludedDomain.user_id == user_id)
            .order_by(UserExcludedDomain.created_at.desc())
        ).all()
    )


def get_excluded_domain(
    db: OrmSession, user_id: int, domain: str
) -> UserExcludedDomain | None:
    return db.get(UserExcludedDomain, (user_id, domain))


def add_excluded_domain(
    db: OrmSession, user_id: int, domain: str
) -> tuple[UserExcludedDomain, bool]:
    """Returns (row, was_created). False on duplicate — the existing row is
    returned unchanged. Caller commits.
    """
    existing = get_excluded_domain(db, user_id, domain)
    if existing is not None:
        return existing, False
    row = UserExcludedDomain(user_id=user_id, domain=domain)
    db.add(row)
    db.flush()
    return row, True


def remove_excluded_domain(db: OrmSession, user_id: int, domain: str) -> bool:
    """Returns True if a row was deleted, False if the domain wasn't in the list."""
    result = db.execute(
        delete(UserExcludedDomain).where(
            UserExcludedDomain.user_id == user_id,
            UserExcludedDomain.domain == domain,
        )
    )
    return bool(result.rowcount)


def is_domain_excluded(db: OrmSession, user_id: int, domain: str) -> bool:
    """Used by the B5.4 batch picker. Domain must be pre-normalized by caller."""
    return get_excluded_domain(db, user_id, domain) is not None
