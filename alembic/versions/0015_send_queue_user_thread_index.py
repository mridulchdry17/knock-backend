"""send_queue: index (user_id, gmail_thread_id) for reply matching

Revision ID: 0015_send_queue_user_thread_index
Revises: 0014_contacts_invalid_reason
Create Date: 2026-05-24

B5.6 reply ingestion matches an inbound message back to the originating send via
`WHERE user_id = ? AND gmail_thread_id = ? ORDER BY sent_at DESC`
(reply_ingestor._match_send_for_reply). send_queue is append-only and grows one
row per email forever, so without a covering index this lookup degrades to a
full table scan on every reply processed. The existing indexes
(`status, scheduled_for`) and (`user_id, status`) don't help this predicate.

gmail_message_id is intentionally NOT indexed — it is never used as a query
predicate (reply idempotency keys off status='REPLIED', not the message id).

Non-destructive: pure CREATE INDEX. Reversible.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_send_queue_user_thread_index"
down_revision: str | Sequence[str] | None = "0014_contacts_invalid_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "idx_queue_user_thread",
        "send_queue",
        ["user_id", "gmail_thread_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_queue_user_thread", table_name="send_queue")
