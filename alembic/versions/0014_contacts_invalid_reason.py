"""contacts: add invalid_reason so the admin dashboard can surface bounces

Revision ID: 0014_contacts_invalid_reason
Revises: 0013_templates_starter_and_updated_at
Create Date: 2026-05-24

When a CSV/manually-curated contact's address bounces, the reply ingestor marks
it is_invalid (so it leaves every user's pool). `invalid_reason` records WHY
("bounce") so the admin dashboard can show "this address bounced — delete it?"
and distinguish it from other invalidations. Nullable; only set when known.

Reversible.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_contacts_invalid_reason"
down_revision: str | Sequence[str] | None = "0013_templates_starter_and_updated_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("contacts") as batch:
        batch.add_column(sa.Column("invalid_reason", sa.String(32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("contacts") as batch:
        batch.drop_column("invalid_reason")
