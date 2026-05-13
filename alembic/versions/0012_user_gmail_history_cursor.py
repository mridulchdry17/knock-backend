"""B5.6: gmail_history_id cursor on users + reply columns on send_queue

Revision ID: 0012_user_gmail_history_cursor
Revises: 0011_email_failures
Create Date: 2026-05-12

Bundled per the B5.6 brief (ONE migration). Three additions:

1. `users.gmail_history_id` (BIGINT, nullable) — the Gmail History API
   watermark per user. The reply ingest cron sets this after each run so the
   next run picks up where we left off (instead of re-fetching the whole
   mailbox or, worse, drowning on the first bootstrap).

2. `send_queue.replied_at` (DateTime(tz=True), nullable) — set when the
   reply ingestor flips a row's status to 'REPLIED'. Source is the Gmail
   message internal_date, NOT the cron run time — we want to surface the
   true reply timestamp in the Inbox UI.

3. `send_queue.reply_is_explicit_stop` (Boolean, nullable) — True if the
   explicit-stop regex matched the reply body. Drives the badge in the
   Inbox card and the lock_status display.

Reversible via batch_alter_table (libSQL/SQLite-safe).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_user_gmail_history_cursor"
down_revision: str | Sequence[str] | None = "0011_email_failures"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("gmail_history_id", sa.BigInteger(), nullable=True))

    with op.batch_alter_table("send_queue") as batch:
        batch.add_column(
            sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("reply_is_explicit_stop", sa.Boolean(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("send_queue") as batch:
        batch.drop_column("reply_is_explicit_stop")
        batch.drop_column("replied_at")

    with op.batch_alter_table("users") as batch:
        batch.drop_column("gmail_history_id")
