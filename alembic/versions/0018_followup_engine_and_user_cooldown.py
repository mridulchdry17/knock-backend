"""followup engine + 30-day per-user contact cooldown

Revision ID: 0018_followup_engine_and_user_cooldown
Revises: 0017_waitlist_intended_tier
Create Date: 2026-06-10

Two features, one migration (additive only, all reversible).

1. **30-day per-user "already emailed" cooldown.** New table
   `user_contact_cooldown` records the (user, contact) → cooldown_until window.
   The daily picker excludes contacts whose cooldown hasn't expired. Other
   users on the platform are unaffected (the existing 36h platform cohort
   hold + 2-day per-user reply lock are separate concerns).

2. **Follow-up engine.** Today_batch_items gain `kind` ('initial'|'followup'),
   `parent_send_queue_id` (link back to the originating send), `followup_index`
   (1 or 2), and `skip_reason` (already referenced by send_worker but never
   in DDL — fixing the silent-no-op alongside). Send_queue gains
   `rfc822_message_id` (the actual RFC822 Message-ID header value, not Gmail's
   internal API id — required so follow-ups can set `In-Reply-To` for IMAP/
   non-Gmail recipients). Existing send_queue.kind values widen from
   'INITIAL' to also include 'FOLLOWUP'.

Reversible. Backfill: existing today_batch_items default to kind='initial';
existing send_queue rows leave rfc822_message_id NULL (best-effort threading
via Gmail's threadId still works for those).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_followup_engine_and_user_cooldown"
down_revision: str | Sequence[str] | None = "0017_waitlist_intended_tier"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. user_contact_cooldown — per-user "I already emailed this contact" hold.
    op.create_table(
        "user_contact_cooldown",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("contact_id", sa.Integer(), sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "contact_id", name="pk_user_contact_cooldown"),
    )
    op.create_index(
        "idx_ucc_user_until",
        "user_contact_cooldown",
        ["user_id", "cooldown_until"],
    )

    # 2. today_batch_items — followup support + skip_reason (silent-no-op fix).
    # FK on parent_send_queue_id is application-managed (Turso doesn't enforce
    # FKs by default, and naming inline FKs inside batch_alter_table is fragile
    # with libsql). The `idx_tbi_parent` index gives the cancel-on-reply scan
    # the same perf as the FK would.
    with op.batch_alter_table("today_batch_items") as batch:
        batch.add_column(
            sa.Column("kind", sa.String(16), nullable=False, server_default="initial")
        )
        batch.add_column(sa.Column("parent_send_queue_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("followup_index", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("skip_reason", sa.String(64), nullable=True))
    op.create_index(
        "idx_tbi_parent",
        "today_batch_items",
        ["parent_send_queue_id"],
    )

    # 3. send_queue — RFC822 Message-ID for cross-MUA threading.
    with op.batch_alter_table("send_queue") as batch:
        batch.add_column(sa.Column("rfc822_message_id", sa.String(255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("send_queue") as batch:
        batch.drop_column("rfc822_message_id")
    op.drop_index("idx_tbi_parent", "today_batch_items")
    with op.batch_alter_table("today_batch_items") as batch:
        batch.drop_column("skip_reason")
        batch.drop_column("followup_index")
        batch.drop_column("parent_send_queue_id")
        batch.drop_column("kind")
    op.drop_index("idx_ucc_user_until", "user_contact_cooldown")
    op.drop_table("user_contact_cooldown")
