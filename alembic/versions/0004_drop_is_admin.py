"""drop_is_admin: tier replaces the boolean

Revision ID: 0004_drop_is_admin
Revises: 0003_users_extensions
Create Date: 2026-05-05

The `users.is_admin` boolean is replaced by `users.tier == 'super_admin'` in
Phase 4. By the time this migration runs, all code reads `tier` and nothing
writes `is_admin`. Safe to drop.

Deploy order matters: this migration must run AFTER the Phase 4 application
code is deployed (the code that no longer reads is_admin). Inside a single
`alembic upgrade head` invocation, 0003 → 0004 runs in sequence, but the app
code being live is what makes 0004 safe.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_drop_is_admin"
down_revision: str | Sequence[str] | None = "0003_users_extensions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("is_admin")


def downgrade() -> None:
    # Restore the column with the same default as 0001_init. Existing rows get
    # is_admin=false; if you need any user re-flagged as admin after rollback,
    # do it manually (or rely on the SUPER_ADMIN_EMAILS env-var allowlist that
    # would still be active under the old code path).
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "is_admin",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
            )
        )
