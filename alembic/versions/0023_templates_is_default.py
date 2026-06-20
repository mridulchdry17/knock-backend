"""templates: add is_default flag for the autopilot template choice

Revision ID: 0023_templates_is_default
Revises: 0022_refresh_tokens
Create Date: 2026-06-21

Today autopilot picks the user's "first starter, else oldest" template
implicitly — there's no UI for the user to pick which one. As soon as we
expose autopilot to real users that becomes a trust problem (a 21yo
firing 15 emails/day to dream recruiters needs to know WHICH template is
going out, and they need a one-click way to switch).

Adds `is_default` to templates. Exactly one row per user should be flagged
true at any time — the application enforces that invariant (single
default per user) in the new set-default endpoint via an atomic UPDATE.
A DB-level constraint isn't worth it for this v0 (would require partial-
unique-index magic + migration headaches for legacy rows).

`templates_repo.default_for_user` reads `is_default DESC` first, so the
explicit flag wins over the implicit fallback chain. Existing users
without a default row continue to get the same template they were
getting before — the fallback ordering is preserved.

Reversible.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_templates_is_default"
down_revision: str | Sequence[str] | None = "0022_refresh_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain ADD COLUMN (no batch_alter_table) — libsql/SQLite supports
    # in-place ALTER TABLE ADD COLUMN for nullable / defaulted columns,
    # which is what we want. batch_alter_table would do a destructive
    # table recreate and that trips the self-referential FK on
    # parent_template_id (Hrana: "FOREIGN KEY constraint failed").
    op.add_column(
        "templates",
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("templates", "is_default")
