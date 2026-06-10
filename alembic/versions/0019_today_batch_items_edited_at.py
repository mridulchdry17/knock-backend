"""today_batch_items: add edited_at to distinguish manual edits from template renders

Revision ID: 0019_today_batch_items_edited_at
Revises: 0018_followup_engine_and_user_cooldown
Create Date: 2026-06-11

Batch-template apply needs to know which cards the user touched manually —
those should be preserved when the user rewrites the whole batch with a new
template. `edited_at` is set by PATCH /today/items whenever subject or body
are explicitly passed (the template-swap path does NOT set it; it's a
template render, not a manual edit). NULL = pristine template render.

Reversible.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_today_batch_items_edited_at"
down_revision: str | Sequence[str] | None = "0018_followup_engine_and_user_cooldown"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("today_batch_items") as batch:
        batch.add_column(
            sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("today_batch_items") as batch:
        batch.drop_column("edited_at")
