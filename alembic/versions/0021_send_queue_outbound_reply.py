"""send_queue: persist the user's last outbound reply (sent from Knock)

Revision ID: 0021_send_queue_outbound_reply
Revises: 0020_send_queue_reply_body
Create Date: 2026-06-19

POST /api/v1/inbox/{id}/reply sends through Gmail (threaded on the original
conversation). Gmail is the source of truth, but the detail view needs to show
the user their own reply on the very next page load — without polling Gmail
back for our own send. Mirror the inbound-reply denorm from migration 0020:
one slot for the most recent outbound reply on the thread.

v0 only stores the last outbound reply; sending a second one overwrites the
first. The proper inbox_messages table is a future migration.

Reversible.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021_send_queue_outbound_reply"
down_revision: str | Sequence[str] | None = "0020_send_queue_reply_body"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("send_queue") as batch:
        batch.add_column(sa.Column("outbound_reply_text", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("outbound_reply_sent_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("send_queue") as batch:
        batch.drop_column("outbound_reply_sent_at")
        batch.drop_column("outbound_reply_text")
