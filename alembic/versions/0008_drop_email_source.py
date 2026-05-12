"""drop_email_source: contacts.email_source was redundant with contacts.source

Revision ID: 0008_drop_email_source
Revises: 0007_contacts_notes_and_source
Create Date: 2026-05-12

Background: 0001_init created `contacts.email_source` (intended values:
'manual' | 'hunter' | 'guess' — i.e. how we obtained the email address).
0007_contacts_notes_and_source added `contacts.source` for operational
provenance ('linkedin-scrape' / '2026-iit-fair' / 'manual-research' / etc.).

After B5.1b fixed the upload service to write `source` (was incorrectly
hitting `email_source`), nothing in the codebase reads or writes
`email_source` anymore. Verified via grep: only the column declaration
in the model and the original 0001_init line referenced it.

For v0 with manual admin uploads, the distinction between
"where we found the person" and "how we got their email" collapses
to the same string. If a future scraper needs to differentiate
(e.g., person discovered via LinkedIn but email obtained via Hunter),
re-adding a column is one migration.

Migration is data-preserving: copies any non-null `email_source` into
`source` (only when `source` is currently null) before dropping the
column, so no admin curation is lost.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_drop_email_source"
down_revision: str | Sequence[str] | None = "0007_contacts_notes_and_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Preserve any existing email_source values by copying into source where source is empty.
    op.execute(
        "UPDATE contacts SET source = email_source "
        "WHERE source IS NULL AND email_source IS NOT NULL"
    )
    with op.batch_alter_table("contacts") as batch:
        batch.drop_column("email_source")


def downgrade() -> None:
    import sqlalchemy as sa

    with op.batch_alter_table("contacts") as batch:
        batch.add_column(sa.Column("email_source", sa.String(16), nullable=True))
    # We don't reverse-copy from `source` because the original distinction
    # (manual/hunter/guess vs operational provenance) is lost. Field comes
    # back empty; any code relying on it must handle null.
