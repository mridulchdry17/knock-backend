"""email_failures — permanent record of send-side errors.

Distinct from `email_logs`: this is a focused, indexed view powering the
admin failures dashboard. One row per failed send attempt.

`failure_kind` is one of:
  - gmail_auth_revoked  (also flips user.gmail_disconnected via service)
  - quota_exceeded
  - recipient_rejected
  - transient
  - unknown
"""
from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._mixins import CreatedAtMixin


class EmailFailure(Base, CreatedAtMixin):
    __tablename__ = "email_failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    today_batch_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("today_batch_items.id", ondelete="SET NULL")
    )
    company_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    failure_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    gmail_error_code: Mapped[str | None] = mapped_column(String(64))

    # `created_at` provided by CreatedAtMixin.

    __table_args__ = (
        Index("idx_email_failures_user_created", "user_id", "created_at"),
        Index("idx_email_failures_kind_created", "failure_kind", "created_at"),
        Index("idx_email_failures_company", "company_domain"),
    )
