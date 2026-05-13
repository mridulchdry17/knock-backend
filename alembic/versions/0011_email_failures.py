"""email_failures table + send_queue reshape for company-grouped sends

Revision ID: 0011_email_failures
Revises: 0010_today_batch_items
Create Date: 2026-05-12

Two related changes bundled per the B5.5 brief (ONE migration, not two):

1. NEW table `email_failures` — permanent record of every send-side error.
   Distinct from `email_logs` (which is the generic audit log) because admin
   wants a focused, indexed view "what's failing right now and for whom" and
   we don't want to scan the giant log table. Retention is out of scope.

2. RESHAPE `send_queue` — the 0001_init shape was modeled around 1 email per
   contact with required campaign_id + template_id. Phase 5 collapses to
   1 email per company with TO+CC of up to 5 contacts and no campaigns yet.
   We:
     - add `today_batch_item_id` (FK back to the planning row)
     - add `to_contact_id` (the primary recipient) and `cc_contact_ids` (JSON)
     - add `gmail_message_id` and `gmail_thread_id` (needed by B5.6 reply matching)
     - add `subject` and `body_text` (the actual content, denormalized for audit)
     - drop NOT NULL on campaign_id / template_id (no campaigns in v0)
     - keep `contact_id` (legacy column) and backfill it = to_contact_id on insert
       so any old reader of send_queue doesn't break.

   Existing send_queue rows in dev are empty (worker hasn't shipped); we do a
   non-destructive ALTER via batch_alter_table.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_email_failures"
down_revision: str | Sequence[str] | None = "0010_today_batch_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── email_failures ───────────────────────────────────────────────
    op.create_table(
        "email_failures",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "today_batch_item_id",
            sa.Integer,
            sa.ForeignKey("today_batch_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("company_domain", sa.String(255), nullable=False),
        sa.Column("failure_kind", sa.String(32), nullable=False),
        sa.Column("error_message", sa.Text, nullable=False),
        sa.Column("gmail_error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_email_failures_user_created",
        "email_failures",
        ["user_id", "created_at"],
    )
    op.create_index(
        "idx_email_failures_kind_created",
        "email_failures",
        ["failure_kind", "created_at"],
    )
    op.create_index(
        "idx_email_failures_company", "email_failures", ["company_domain"]
    )

    # ── users.gmail_disconnected flag ────────────────────────────────
    # Set True by the worker when Gmail returns invalid_grant/failedPrecondition.
    # Surfaces "reconnect Gmail" CTA on the frontend.
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "gmail_disconnected",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    # ── send_queue reshape ───────────────────────────────────────────
    with op.batch_alter_table("send_queue") as batch:
        # Loosen the legacy FKs — no campaigns/templates in v0.
        batch.alter_column("campaign_id", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("template_id", existing_type=sa.Integer(), nullable=True)

        # Add the company-grouped fields.
        batch.add_column(sa.Column("today_batch_item_id", sa.Integer, nullable=True))
        batch.add_column(sa.Column("to_contact_id", sa.Integer, nullable=True))
        batch.create_foreign_key(
            "fk_send_queue_today_batch_item",
            "today_batch_items",
            ["today_batch_item_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_send_queue_to_contact",
            "contacts",
            ["to_contact_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.add_column(
            sa.Column(
                "cc_contact_ids",
                sa.Text,
                nullable=False,
                server_default="[]",
            )
        )
        batch.add_column(sa.Column("company_domain", sa.String(255), nullable=True))
        batch.add_column(sa.Column("subject", sa.String(998), nullable=True))
        batch.add_column(sa.Column("body_text", sa.Text, nullable=True))
        batch.add_column(sa.Column("gmail_message_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("gmail_thread_id", sa.String(128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("send_queue") as batch:
        batch.drop_constraint("fk_send_queue_to_contact", type_="foreignkey")
        batch.drop_constraint("fk_send_queue_today_batch_item", type_="foreignkey")
        batch.drop_column("gmail_thread_id")
        batch.drop_column("gmail_message_id")
        batch.drop_column("body_text")
        batch.drop_column("subject")
        batch.drop_column("company_domain")
        batch.drop_column("cc_contact_ids")
        batch.drop_column("to_contact_id")
        batch.drop_column("today_batch_item_id")
        batch.alter_column("template_id", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("campaign_id", existing_type=sa.Integer(), nullable=False)

    with op.batch_alter_table("users") as batch:
        batch.drop_column("gmail_disconnected")

    op.drop_index("idx_email_failures_company", table_name="email_failures")
    op.drop_index("idx_email_failures_kind_created", table_name="email_failures")
    op.drop_index("idx_email_failures_user_created", table_name="email_failures")
    op.drop_table("email_failures")
