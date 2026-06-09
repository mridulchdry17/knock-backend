"""contacts: scraped_pattern + globally unique email

Revision ID: 0005_contacts_scraped_pattern
Revises: 0004_drop_is_admin
Create Date: 2026-05-12

Phase 5 (B5.1) requires:

1. `contacts.scraped_pattern` (nullable string) — set by the future scraper to
   record which email-guess format produced the address (e.g. ``firstname.lastname``).
   Used by B5.5's retry-alt-format logic after a bounce.

2. Globally unique `contacts.email`. The original schema had a composite
   ``uq_contact_company_email`` allowing the same email under different company
   rows, but the Phase 5 send model keys the 36h cooldown by ``company_domain``
   and treats email as the global dedup key for the admin-curated pool. Same
   person on multiple company rows is wrong by construction; enforce it in the
   table.

The new uniqueness also removes ambiguity in the bulk upsert path
(``get_by_email`` is the canonical lookup).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_contacts_scraped_pattern"
down_revision: str | Sequence[str] | None = "0004_drop_is_admin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("contacts") as batch:
        batch.add_column(sa.Column("scraped_pattern", sa.String(64), nullable=True))
        batch.drop_constraint("uq_contact_company_email", type_="unique")
        batch.create_unique_constraint("uq_contacts_email", ["email"])


def downgrade() -> None:
    with op.batch_alter_table("contacts") as batch:
        batch.drop_constraint("uq_contacts_email", type_="unique")
        batch.create_unique_constraint(
            "uq_contact_company_email", ["company_id", "email"]
        )
        batch.drop_column("scraped_pattern")
