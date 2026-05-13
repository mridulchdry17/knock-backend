from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._mixins import CreatedAtMixin


class SendQueue(Base, CreatedAtMixin):
    """Append-only record of every Knock-dispatched email.

    Originally modeled as a per-contact PENDING queue (0001_init); reshaped in
    migration 0011 to support the Phase 5 company-grouped send model:
      - 1 row per email sent
      - `to_contact_id` is the primary recipient
      - `cc_contact_ids` JSON-encodes the up-to-4 CC contacts
      - `today_batch_item_id` back-references the planning row
      - `gmail_message_id` / `gmail_thread_id` used by B5.6 reply matching

    `campaign_id` / `template_id` are nullable now — v0 has no campaigns table
    in active use. Legacy `contact_id` is set = `to_contact_id` on insert by
    the worker for any consumer still reading the old shape.
    """

    __tablename__ = "send_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), nullable=False)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("templates.id"))

    # Phase 5 additions (migration 0011) ───────────────────────────────
    today_batch_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("today_batch_items.id", ondelete="SET NULL")
    )
    to_contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    # JSON-encoded list[int] of CC contact IDs.
    cc_contact_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    company_domain: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(String(998))
    body_text: Mapped[str | None] = mapped_column(Text)
    gmail_message_id: Mapped[str | None] = mapped_column(String(128))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(128))

    # Legacy queue fields ─────────────────────────────────────────────
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="INITIAL")
    # INITIAL | FOLLOWUP
    in_reply_to_thread_id: Mapped[str | None] = mapped_column(String(128))

    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    # PENDING | SENT | FAILED | SKIPPED | LOCKED

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(1024))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_queue_due", "status", "scheduled_for"),
        Index("idx_queue_user_status", "user_id", "status"),
    )
