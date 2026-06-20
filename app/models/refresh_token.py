from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base


class RefreshToken(Base):
    """Long-lived refresh token stored as an HttpOnly cookie on the browser.

    PK is the raw token value — same convention as `sessions.id`. We look up
    rows by exact match on the secret presented in the Cookie header; there
    is no separate hash column. (For an HttpOnly cookie this is acceptable —
    the only way an attacker reads the token is by breaching the DB at rest,
    in which case a hash would also be brute-forceable for any reasonable
    token length.)

    Rotation chain:
      - One LOGIN starts a `family_id`.
      - Each REFRESH revokes the current row (sets `revoked_at` + `replaced_by_id`)
        and inserts a NEW row in the same family.
      - If a token whose `replaced_by_id` is already set is ever presented
        again (theft / replay), the validate code MUST revoke every row in
        the family. The legitimate device will get logged out and re-auth;
        the attacker's stolen token also becomes useless.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    family_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Soft-delete marker. Set on logout, on rotation (the just-rotated row),
    # and on whole-family revocation when reuse is detected.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Plain String (not FK) so we don't pay the libsql/SQLite batch_alter
    # circular-FK cost. We only walk the chain forward to detect reuse.
    replaced_by_id: Mapped[str | None] = mapped_column(String(64))

    user_agent: Mapped[str | None] = mapped_column(String(512))
    ip: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_refresh_tokens_family_id", "family_id"),
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_expires_at", "expires_at"),
    )
