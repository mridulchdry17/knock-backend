"""waitlist: add intended_tier so admin can pre-mark someone as paid-on-arrival

Revision ID: 0017_waitlist_intended_tier
Revises: 0016_waitlist_approved_at
Create Date: 2026-05-25

The Allow-in action approves a waitlist entry, but the resulting tier was
hardcoded to 'free' — even after the person signed in you had to bounce them
through `/admin/users` for a separate Promote-to-paid click. `intended_tier`
lets the admin pre-mark the entry: 'free' (default) or 'paid'. The auth/claim
paths and the admin Allow handler honour it on sign-in.

Backfill: all existing rows → 'free' (the current behaviour, unchanged). NOT
NULL with a CHECK so we can't end up in a half-set state.

Reversible.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_waitlist_intended_tier"
down_revision: str | Sequence[str] | None = "0016_waitlist_approved_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("waitlist") as batch:
        batch.add_column(
            sa.Column(
                "intended_tier",
                sa.String(16),
                nullable=False,
                server_default="free",
            )
        )
        batch.create_check_constraint(
            "ck_waitlist_intended_tier",
            "intended_tier IN ('free', 'paid')",
        )


def downgrade() -> None:
    with op.batch_alter_table("waitlist") as batch:
        batch.drop_constraint("ck_waitlist_intended_tier", type_="check")
        batch.drop_column("intended_tier")
