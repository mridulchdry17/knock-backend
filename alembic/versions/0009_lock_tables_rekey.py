"""lock tables: re-key global_contact_lock to company_domain + add user/platform locks

Revision ID: 0009_lock_tables_rekey
Revises: 0008_drop_email_source
Create Date: 2026-05-12

Implements the Phase 5 three-tier lock model (project_phase5_send_model.md):

  1. `global_contact_lock` — re-keyed from `contact_id` (the 0001_init schema) to
     `company_domain`. The 0001_init shape was wrong for the company-grouped
     send model: after any user emails @acme.com, the platform-wide 36h
     cooldown applies per DOMAIN, not per contact. Existing rows are dev-seed
     locks only (no production lock data); we drop and recreate rather than
     attempt a per-row migration.

  2. `user_company_locks` — NEW. Per-user, per-company 30-day reply lock. When
     ANY reply lands on a user's thread to @acme.com, this user is locked
     out of @acme.com for 30 days (rolling — extends on every reply). Other
     users on Knock can still email @acme.com (subject to the 36h platform
     cooldown). B5.6 will write to this on reply ingestion.

  3. `platform_company_locks` — NEW. Platform-wide PERMANENT stop. Set when
     B5.6's explicit-stop regex fires on a reply body ("unsubscribe",
     "stop emailing", etc.). No auto-expiry; super_admin clears manually.

batch_alter_table is used for SQLite compatibility; downgrade is reversible
back to the original `global_contact_lock(contact_id)` shape.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_lock_tables_rekey"
down_revision: str | Sequence[str] | None = "0008_drop_email_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Step 1: re-key global_contact_lock ──────────────────────────────
    # Drop the contact_id-keyed shape. Existing rows are dev-seed only; the
    # send worker (B5.5) has not shipped, so there are no production locks
    # to preserve. If future production data needs preserving, lift the
    # contact_id → company_domain mapping via JOIN in a separate migration.
    op.drop_index("idx_lock_until", table_name="global_contact_lock")
    op.drop_table("global_contact_lock")

    op.create_table(
        "global_contact_lock",
        sa.Column("company_domain", sa.String(255), primary_key=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_locked_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("idx_global_lock_until", "global_contact_lock", ["locked_until"])

    # ── Step 2: user_company_locks (30-day per-user reply lock) ─────────
    op.create_table(
        "user_company_locks",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("company_domain", sa.String(255), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "is_permanent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint(
            "user_id", "company_domain", name="pk_user_company_locks"
        ),
    )
    op.create_index(
        "idx_ucl_user_domain", "user_company_locks", ["user_id", "company_domain"]
    )
    op.create_index("idx_ucl_locked_until", "user_company_locks", ["locked_until"])

    # ── Step 3: platform_company_locks (permanent stop) ─────────────────
    op.create_table(
        "platform_company_locks",
        sa.Column("company_domain", sa.String(255), primary_key=True),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("platform_company_locks")

    op.drop_index("idx_ucl_locked_until", table_name="user_company_locks")
    op.drop_index("idx_ucl_user_domain", table_name="user_company_locks")
    op.drop_table("user_company_locks")

    op.drop_index("idx_global_lock_until", table_name="global_contact_lock")
    op.drop_table("global_contact_lock")

    # Restore original 0001_init shape (best-effort; lock state is lost).
    op.create_table(
        "global_contact_lock",
        sa.Column(
            "contact_id",
            sa.Integer(),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "locked_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_lock_until", "global_contact_lock", ["locked_until"])
