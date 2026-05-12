"""contacts_notes_and_source: notes/source columns on contacts + user_contact_notes table

Revision ID: 0007_contacts_notes_and_source
Revises: 0006_user_preferences
Create Date: 2026-05-12

B5.1b fixup on top of B5.1. Two parallel notes surfaces:

  1. `Contact.notes` / `Contact.source` — admin/shared metadata. Populated by
     the CSV upload (already accepts these columns; B5.1b makes them persist)
     or the future scraper. Read by every user who sees the contact.

     `source` is operational provenance (linkedin-scrape, 2026-iit-fair,
     referral-aman, manual-research). Distinct from existing `email_source`
     which is the email-discovery mechanism (manual|hunter|guess).

  2. `user_contact_notes` — per-user, private. Composite-PK (user_id, contact_id)
     mirrors the `user_excluded_domains` pattern from migration 0006. Each
     student records their own observations about a contact, isolated from
     other users.

Both surfaces survive contact-row updates; the CSV upload selectively
overwrites `Contact.notes` only if the new value is non-null, preserving
prior admin curation.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_contacts_notes_and_source"
down_revision: str | Sequence[str] | None = "0006_user_preferences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("contacts") as batch:
        batch.add_column(sa.Column("notes", sa.Text(), nullable=True))
        batch.add_column(sa.Column("source", sa.String(64), nullable=True))

    op.create_table(
        "user_contact_notes",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            sa.Integer(),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "contact_id", name="pk_user_contact_notes"),
    )
    op.create_index(
        "ix_user_contact_notes_user_id",
        "user_contact_notes",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_contact_notes_user_id", table_name="user_contact_notes")
    op.drop_table("user_contact_notes")

    with op.batch_alter_table("contacts") as batch:
        batch.drop_column("source")
        batch.drop_column("notes")
