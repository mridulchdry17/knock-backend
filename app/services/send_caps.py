"""Canonical daily-send cap resolution.

Shared by the batch picker (caps batch *size* at generation) and the send
worker (enforces the cap at *dispatch*). Keeping a single source prevents the
two from drifting — the original bug was the cap living only in the picker, so
the worker would happily send more than `cap` rows if extra 'ready' items
appeared (lazy-gen + manual re-ready + a lowered cap).
"""
from __future__ import annotations

from app.models import User

# free=7, paid=20. super_admin treated as paid for v0 (devs need realistic
# send volume to test). A tier absent from this map resolves to 0 (e.g.
# 'pending' — never generates or sends).
TIER_DEFAULT_CAPS: dict[str, int] = {
    "free": 7,
    "paid": 20,
    "super_admin": 20,
}


def resolve_daily_cap(user: User) -> int:
    """Per-user daily send cap. An explicit `daily_limit > 0` override beats
    the tier default; otherwise fall back to the tier default (0 if no tier)."""
    if user.daily_limit and user.daily_limit > 0:
        return user.daily_limit
    return TIER_DEFAULT_CAPS.get(user.tier, 0)
