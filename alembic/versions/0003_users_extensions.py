"""users_extensions: tier + waitlist_email + tier_set_at

Revision ID: 0003_users_extensions
Revises: 0002_waitlist
Create Date: 2026-05-05

Adds the three columns Phase 4 needs to express the soft-gate + admin-approval
auth model:

  - waitlist_email TEXT NULL UNIQUE — which waitlist email this user claimed.
    Nullable for users who haven't onboarded. UNIQUE prevents two users
    claiming the same waitlist row.
  - tier TEXT NOT NULL DEFAULT 'pending' CHECK IN
    ('pending','free','paid','super_admin')
  - tier_set_at TIMESTAMP NULL — audit trail.

Existing rows are backfilled to tier='free' per project decision (super_admin
status comes from the SUPER_ADMIN_EMAILS env-var allowlist on next login,
not from the DB).

SQLite doesn't support adding constraints via ALTER, so we use batch_alter_table
which copy-and-renames the table. Postgres/Turso supports both paths; batch is
a no-op overhead there.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_users_extensions"
down_revision: str | Sequence[str] | None = "0002_waitlist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Phase 1: add columns. server_default='pending' ensures existing rows have
    # a valid value before we install the CHECK.
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("waitlist_email", sa.String(255), nullable=True))
        batch.add_column(
            sa.Column("tier", sa.String(16), nullable=False, server_default="pending")
        )
        batch.add_column(sa.Column("tier_set_at", sa.DateTime(timezone=True), nullable=True))

    # Phase 2: backfill existing users. Project decision: all existing rows
    # become 'free' regardless of is_admin. The SUPER_ADMIN_EMAILS env-var
    # allowlist promotes the right user(s) to 'super_admin' on their next login.
    op.execute("UPDATE users SET tier='free'")

    # Phase 3: install constraints. Separate batch — SQLite needs the column
    # populated with valid values before the CHECK is added.
    with op.batch_alter_table("users") as batch:
        batch.create_unique_constraint("uq_users_waitlist_email", ["waitlist_email"])
        batch.create_check_constraint(
            "ck_users_tier",
            "tier IN ('pending','free','paid','super_admin')",
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("ck_users_tier", type_="check")
        batch.drop_constraint("uq_users_waitlist_email", type_="unique")
        batch.drop_column("tier_set_at")
        batch.drop_column("tier")
        batch.drop_column("waitlist_email")
