"""templates: add is_starter + updated_at for the per-user template library

Revision ID: 0013_templates_starter_and_updated_at
Revises: 0012_user_gmail_history_cursor
Create Date: 2026-05-23

Wires the templates feature onto the existing `templates` table (created in
0001_init but never exposed via an API). The frontend F6 contract needs two
fields the original table lacked:

  - `is_starter` (bool) — True for the 3 templates seeded on first login, so
    the UI can badge them and we can tell seeded vs user-authored apart.
  - `updated_at` (datetime) — the contract returns it; the model gains the
    onupdate=utcnow behavior.

`used_count` and `reply_rate` from the contract are computed at read time
(used_count = today_batch_items referencing the template; reply_rate = null
in v0), so no columns for those.

Reversible via batch_alter_table (libSQL/SQLite-safe).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_templates_starter_and_updated_at"
down_revision: str | Sequence[str] | None = "0012_user_gmail_history_cursor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("templates") as batch:
        batch.add_column(
            sa.Column(
                "is_starter",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("templates") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("is_starter")
