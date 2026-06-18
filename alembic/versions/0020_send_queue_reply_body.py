"""send_queue: persist the latest inbound reply body for the inbox detail view

Revision ID: 0020_send_queue_reply_body
Revises: 0019_today_batch_items_edited_at
Create Date: 2026-06-19

GET /api/v1/inbox/{id} shows the conversation we had with the recruiter — our
original outbound plus their reply on the same thread. Today the reply ingestor
flips status=REPLIED and stamps replied_at but throws the reply body away;
without it the detail view has nothing to render.

Denormalize the LAST reply onto the send_queue row instead of standing up a
new inbox_messages table. v0 model is one outbound per company and the surface
shows one back-and-forth — a column-level denorm is the right weight for that.
Multi-turn threading will get its own table later.

Reversible.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_send_queue_reply_body"
down_revision: str | Sequence[str] | None = "0019_today_batch_items_edited_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("send_queue") as batch:
        batch.add_column(sa.Column("reply_body_text", sa.Text(), nullable=True))
        batch.add_column(sa.Column("reply_from_email", sa.String(255), nullable=True))
        batch.add_column(
            sa.Column("reply_internal_date", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("send_queue") as batch:
        batch.drop_column("reply_internal_date")
        batch.drop_column("reply_from_email")
        batch.drop_column("reply_body_text")
