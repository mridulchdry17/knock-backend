"""user_preferences: target metadata + notification toggles + autopilot + excluded domains

Revision ID: 0005_user_preferences
Revises: 0004_drop_is_admin
Create Date: 2026-05-12

Backs the `/api/v1/preferences` endpoints (B5.2). Two surfaces:

  1. New columns on `users`:
     - target_role / target_industries / target_location — free-text targeting
       metadata collected at /preferences. JSON list of strings for industries.
       Not yet read by the batch picker in v0 (locked product decision; see
       project_targeting_model.md); stored so we can light it up in v1.
     - notify_gmail_disconnect / notify_daily_summary — notification toggles.
     - autopilot_enabled / autopilot_paused_at / autopilot_auto_pause_on_reply
       — Phase 5 autopilot state. The first two were spec'd in Phase 4 but
       never actually landed; folding them in here keeps the schema honest.
     - resume_url — Drive (or any URL) link; we never store the PDF itself.

  2. New table `user_excluded_domains` — composite-PK (user_id, domain). This
     is the ONE preference that drives v0 behavior: the B5.4 batch picker
     filters contacts on it.

SQLite batch_alter_table is used for the column additions so this migration
applies cleanly on the test/dev sqlite DB and on prod libsql.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_user_preferences"
down_revision: str | Sequence[str] | None = "0004_drop_is_admin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("target_role", sa.String(255), nullable=True))
        # JSON-encoded list[str]; deserialized at the Pydantic boundary.
        batch.add_column(sa.Column("target_industries", sa.Text(), nullable=True))
        batch.add_column(sa.Column("target_location", sa.String(255), nullable=True))
        batch.add_column(
            sa.Column(
                "notify_gmail_disconnect",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch.add_column(
            sa.Column(
                "notify_daily_summary",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch.add_column(
            sa.Column(
                "autopilot_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column("autopilot_paused_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "autopilot_auto_pause_on_reply",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch.add_column(sa.Column("resume_url", sa.String(2048), nullable=True))

    op.create_table(
        "user_excluded_domains",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "domain", name="pk_user_excluded_domains"),
    )
    op.create_index(
        "ix_user_excluded_domains_domain",
        "user_excluded_domains",
        ["domain"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_excluded_domains_domain", table_name="user_excluded_domains")
    op.drop_table("user_excluded_domains")

    with op.batch_alter_table("users") as batch:
        batch.drop_column("resume_url")
        batch.drop_column("autopilot_auto_pause_on_reply")
        batch.drop_column("autopilot_paused_at")
        batch.drop_column("autopilot_enabled")
        batch.drop_column("notify_daily_summary")
        batch.drop_column("notify_gmail_disconnect")
        batch.drop_column("target_location")
        batch.drop_column("target_industries")
        batch.drop_column("target_role")
