"""init

Revision ID: 0001_init
Revises:
Create Date: 2026-05-03

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_init"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("full_name", sa.String(255)),
        sa.Column("google_sub", sa.String(64), unique=True),
        sa.Column("google_refresh_token", sa.String),
        sa.Column("google_access_token", sa.String),
        sa.Column("google_token_expiry", sa.DateTime(timezone=True)),
        sa.Column("google_scopes", sa.String),
        sa.Column("google_connected_at", sa.DateTime(timezone=True)),
        sa.Column("daily_limit", sa.Integer, nullable=False, server_default="20"),
        sa.Column("sent_today", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_reset_date", sa.Date),
        sa.Column("sender_signature_name", sa.String(255)),
        sa.Column("sender_signature_city", sa.String(255)),
        sa.Column("is_admin", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_suspended", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("ip", sa.String(64)),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    op.create_table(
        "companies",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("domain", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("article_url", sa.String(1024)),
        sa.Column("funding_stage", sa.String(32)),
        sa.Column("industry", sa.String(64)),
        sa.Column("description", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_companies_stage", "companies", ["funding_stage"])
    op.create_index("idx_companies_industry", "companies", ["industry"])

    op.create_table(
        "contacts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("role", sa.String(128)),
        sa.Column("email", sa.String(255)),
        sa.Column("email_source", sa.String(16)),
        sa.Column("email_confidence", sa.Integer),
        sa.Column("email_verified", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_invalid", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("linkedin_url", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "email", name="uq_contact_company_email"),
    )
    op.create_index("idx_contacts_company", "contacts", ["company_id"])
    op.create_index("ix_contacts_email", "contacts", ["email"])

    op.create_table(
        "templates",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("body", sa.String, nullable=False),
        sa.Column("is_followup", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("parent_template_id", sa.Integer, sa.ForeignKey("templates.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_templates_user_id", "templates", ["user_id"])

    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("template_id", sa.Integer, sa.ForeignKey("templates.id"), nullable=False),
        sa.Column("followup_template_id", sa.Integer, sa.ForeignKey("templates.id")),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("queued_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sent_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("replied_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("bounced_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_campaigns_user_id", "campaigns", ["user_id"])

    op.create_table(
        "send_queue",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("contact_id", sa.Integer, sa.ForeignKey("contacts.id"), nullable=False),
        sa.Column("campaign_id", sa.Integer, sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("template_id", sa.Integer, sa.ForeignKey("templates.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("in_reply_to_thread_id", sa.String(128)),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(1024)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_queue_due", "send_queue", ["status", "scheduled_for"])
    op.create_index("idx_queue_user_status", "send_queue", ["user_id", "status"])

    op.create_table(
        "user_contact_map",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("contact_id", sa.Integer, sa.ForeignKey("contacts.id"), nullable=False),
        sa.Column("campaign_id", sa.Integer, sa.ForeignKey("campaigns.id")),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("gmail_thread_id", sa.String(128)),
        sa.Column("gmail_message_id", sa.String(128)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_followup_at", sa.DateTime(timezone=True)),
        sa.Column("followup_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reply_detected_at", sa.DateTime(timezone=True)),
        sa.Column("bounce_detected_at", sa.DateTime(timezone=True)),
        sa.Column("last_action_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "contact_id", name="uq_ucm_user_contact"),
    )
    op.create_index("idx_ucm_user_status", "user_contact_map", ["user_id", "status"])
    op.create_index("ix_user_contact_map_gmail_thread_id", "user_contact_map", ["gmail_thread_id"])
    op.create_index(
        "idx_ucm_followup_scan",
        "user_contact_map",
        ["status", "sent_at", "followup_count"],
    )

    op.create_table(
        "global_contact_lock",
        sa.Column("contact_id", sa.Integer, sa.ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("locked_by_user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_lock_until", "global_contact_lock", ["locked_until"])

    op.create_table(
        "email_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("contact_id", sa.Integer, sa.ForeignKey("contacts.id")),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("metadata", sa.String),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_logs_user_time", "email_logs", ["user_id", "timestamp"])


def downgrade() -> None:
    op.drop_table("email_logs")
    op.drop_table("global_contact_lock")
    op.drop_table("user_contact_map")
    op.drop_table("send_queue")
    op.drop_table("campaigns")
    op.drop_table("templates")
    op.drop_table("contacts")
    op.drop_table("companies")
    op.drop_table("sessions")
    op.drop_table("users")
