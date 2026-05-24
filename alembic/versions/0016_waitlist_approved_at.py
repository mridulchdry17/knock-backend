"""waitlist: add approved_at so being on the list != being allowed in

Revision ID: 0016_waitlist_approved_at
Revises: 0015_send_queue_user_thread_index
Create Date: 2026-05-25

The public waitlist form and CSV imports drop emails into `waitlist`, but until
now `decide_tier_and_destination` granted tier='free' to ANYONE on the list —
so the "we approve in waves" gate was never enforced. `approved_at` makes the
gate real: a waitlist entry only grants access once a super_admin allows it.

Backfill: every existing row stays NULL (un-approved). Already-active users are
unaffected — they keep their tier via the returning-user branch in auth; only
not-yet-onboarded sign-ins are now gated until explicitly allowed.

Reversible.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_waitlist_approved_at"
down_revision: str | Sequence[str] | None = "0015_send_queue_user_thread_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("waitlist") as batch:
        batch.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("waitlist") as batch:
        batch.drop_column("approved_at")
