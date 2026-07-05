"""autopilot: stop condition columns on users

Revision ID: 0025_autopilot_stop_conditions
Revises: 0024_companies_batch
Create Date: 2026-07-06

Users can pick ONE stop condition (or leave as none):
  - 'replies'  → pause after N replies since autopilot enabled (N ∈ {1,3,5})
  - 'end_date' → pause on/after a chosen calendar date
  - 'budget'   → pause after N total sends since enabled (N ∈ {25,50,100,200})
  - 'none'     → runs until user pauses manually (default)

Regardless of the user's pick, two invisible platform ceilings always apply:
  - 500 total sends since enabled → 'ceiling_sends'
  - 90 days since enabled         → 'ceiling_days'

`autopilot_enabled_at` is the anchor for both user counters and ceilings —
set every time the user toggles autopilot ON, so counters reset each cycle.
Preserved on toggle-off for the audit trail.

`autopilot_paused_reason` is written by the cycle cron when a stop condition
fires. The frontend renders a "resume" CTA + the reason string so the user
knows WHY the system paused them.

The prior `autopilot_auto_pause_on_reply` boolean is left in place for one
release as rollback safety — it's dead in the new logic, backfilled to
`stop_type='replies', stop_at_replies=1` for users who had it on.

Reversible.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_autopilot_stop_conditions"
down_revision: str | Sequence[str] | None = "0024_companies_batch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Plain ADD COLUMN (no batch_alter_table) — libsql/SQLite supports
    # in-place ALTER TABLE ADD COLUMN for nullable / defaulted columns.
    # See 0023_templates_is_default.py for the pattern rationale.
    op.add_column(
        "users",
        sa.Column("autopilot_enabled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "autopilot_stop_type",
            sa.String(16),
            nullable=False,
            server_default="none",
        ),
    )
    op.add_column(
        "users",
        sa.Column("autopilot_stop_at_replies", sa.Integer(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("autopilot_stop_at_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("autopilot_stop_at_budget", sa.Integer(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("autopilot_paused_reason", sa.String(32), nullable=True),
    )

    # ─────────────── data backfill ───────────────
    # Every existing user with the old auto-pause-on-reply flag gets ported
    # to the new model as stop_type='replies', stop_at_replies=1 (the closest
    # equivalent — pause after the very first reply). Users without the flag
    # remain on 'none' (the server_default). The old column is intentionally
    # left in place for one release; new logic ignores it.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE users "
            "SET autopilot_stop_type = 'replies', "
            "    autopilot_stop_at_replies = 1 "
            "WHERE autopilot_auto_pause_on_reply = 1"
        )
    )
    # Anchor existing autopilot users' counters to a best-guess epoch so
    # freshly-migrated rows don't start at NULL and confuse the counter
    # helpers. Prefer updated_at over created_at (more recent → less spurious
    # backlog of sends attributed to autopilot). If neither exists (defensive:
    # both are NOT NULL in this schema), the counter helpers treat NULL as zero.
    bind.execute(
        sa.text(
            "UPDATE users "
            "SET autopilot_enabled_at = COALESCE(updated_at, created_at) "
            "WHERE autopilot_enabled = 1 "
            "  AND autopilot_enabled_at IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_column("users", "autopilot_paused_reason")
    op.drop_column("users", "autopilot_stop_at_budget")
    op.drop_column("users", "autopilot_stop_at_date")
    op.drop_column("users", "autopilot_stop_at_replies")
    op.drop_column("users", "autopilot_stop_type")
    op.drop_column("users", "autopilot_enabled_at")
