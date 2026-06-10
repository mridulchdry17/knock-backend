"""Tests for the canonical daily-cap resolver (app/services/send_caps.py).

Locks the 'tier is a hard ceiling, daily_limit can only lower it' rule. The
default User.daily_limit is DEFAULT_DAILY_LIMIT (20), which is exactly why a
plain 'override beats tier' rule silently broke the free=7 cap — these tests
guard against that regressing.
"""
from __future__ import annotations

import pytest

from app.models import User
from app.services.send_caps import resolve_daily_cap


def _user(tier: str, daily_limit: int) -> User:
    return User(tier=tier, daily_limit=daily_limit)


@pytest.mark.parametrize(
    "tier, daily_limit, expected",
    [
        # Default daily_limit (20) must NOT lift free above its tier ceiling,
        # and now clamps paid to its 15 ceiling too.
        ("free", 20, 7),
        ("paid", 20, 15),
        ("super_admin", 20, 15),
        # Admin throttle below the tier ceiling wins.
        ("free", 3, 3),
        ("paid", 5, 5),
        # Admin can't raise above the tier ceiling.
        ("paid", 50, 15),
        ("free", 100, 7),
        # daily_limit unset / zero → fall back to the tier default.
        ("free", 0, 7),
        ("paid", 0, 15),
        # Unknown / pending tier → 0 (never sends).
        ("pending", 20, 0),
        ("pending", 0, 0),
    ],
)
def test_resolve_daily_cap(tier: str, daily_limit: int, expected: int) -> None:
    assert resolve_daily_cap(_user(tier, daily_limit)) == expected
