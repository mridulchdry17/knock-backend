"""refresh_tokens: long-lived refresh token storage for the two-token auth model

Revision ID: 0022_refresh_tokens
Revises: 0021_send_queue_outbound_reply
Create Date: 2026-06-19

Adds the long-lived half of the new auth model. The existing `sessions` table
remains the short-lived access token; we just shrink its TTL to 15 minutes
(no schema change). This new table holds the long-lived refresh token, which
the browser stores as an HttpOnly cookie — JavaScript cannot read it, so an
XSS in the frontend cannot exfiltrate it.

Key columns:
  - id: the raw urlsafe token (mirrors the sessions table convention — the PK
    IS the secret, no separate hash column). Lookup is by exact match on the
    token sent in the Cookie header. 32-byte token_urlsafe → 43-char string,
    stored in a String(64) for headroom.
  - family_id: groups all refresh tokens issued by a single login event. On
    each rotation we mint a new row in the same family and link the old one
    via `replaced_by_id`. If a token whose `replaced_by_id` is already set is
    ever presented again (theft / replay), we revoke the entire family —
    forces re-login on every device in that family. Built-in anomaly
    detection.
  - replaced_by_id: chain pointer. NULL on the active token in a family.
  - revoked_at: soft-delete marker (logout, family invalidation, expiry).

Indexes:
  - family_id — required for whole-family revocation on suspected reuse.
  - user_id — supports a future "all my sessions" admin page without an
    O(N) scan.
  - expires_at — supports a scheduled GC sweep that hard-deletes long-expired
    rows (NOT done in this PR; index is here so we don't migrate again).

Reversible.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_refresh_tokens"
down_revision: str | Sequence[str] | None = "0021_send_queue_outbound_reply"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("family_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        # Self-referential FK is annoying with batch_alter on libsql/SQLite;
        # store the raw next-token id as a plain String column instead. We
        # only ever follow the chain forward to detect reuse, never join on it.
        sa.Column("replaced_by_id", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
    )
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_expires_at", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
