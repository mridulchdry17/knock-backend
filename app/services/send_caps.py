"""Canonical daily-send cap resolution.

Shared by the batch picker (caps batch *size* at generation) and the send
worker (enforces the cap at *dispatch*). Keeping a single source prevents the
two from drifting — the original bug was the cap living only in the picker, so
the worker would happily send more than `cap` rows if extra 'ready' items
appeared (lazy-gen + manual re-ready + a lowered cap).
"""
from __future__ import annotations

from app.models import User

# free=7, paid=15. super_admin treated as paid for v0 (devs need realistic
# send volume to test). A tier absent from this map resolves to 0 (e.g.
# 'pending' — never generates or sends).
TIER_DEFAULT_CAPS: dict[str, int] = {
    "free": 7,
    "paid": 15,
    "super_admin": 15,
}


def resolve_daily_cap(user: User) -> int:
    """Per-user daily send cap.

    The tier default is a HARD CEILING (free=7, paid=15). `daily_limit` is an
    admin throttle that can only LOWER that ceiling, never raise it — so it's
    `min(tier_default, daily_limit)` when set.

    Why min() and not "override beats tier": `User.daily_limit` defaults to
    `DEFAULT_DAILY_LIMIT` (20) for every row, so a plain "override beats tier"
    rule silently gave free users a cap of 20 and the locked free=7 rule was
    never enforced. With min(), the default-20 is harmless: free clamps to 7
    (min(7,20)=7), paid clamps to 15 (min(15,20)=15). An admin can still
    throttle anyone down (e.g. set 3 → 3), and nobody exceeds their tier
    ceiling. A tier absent from the map → 0.
    """
    tier_default = TIER_DEFAULT_CAPS.get(user.tier, 0)
    if user.daily_limit and user.daily_limit > 0:
        return min(tier_default, user.daily_limit)
    return tier_default
