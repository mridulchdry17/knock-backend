from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._mixins import CreatedAtMixin


class Contact(Base, CreatedAtMixin):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str | None] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    email_confidence: Mapped[int | None] = mapped_column(Integer)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_invalid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Why the contact was invalidated, e.g. "bounce" — lets the admin dashboard
    # surface "this address bounced, delete it?" vs other invalid reasons.
    invalid_reason: Mapped[str | None] = mapped_column(String(32))
    linkedin_url: Mapped[str | None] = mapped_column(String(512))
    # Set by future scraper to record which guess pattern produced this address
    # (e.g. "firstname.lastname"). Used by B5.5 to try an alternate pattern on
    # bounce.
    scraped_pattern: Mapped[str | None] = mapped_column(String(64))
    # Admin/scraper-curated notes shared across ALL users who see this contact
    # ("former IIT-B, MS Stanford"). Read-only for regular users; admin writes
    # via the CSV upload service. Distinct from per-user observations stored
    # in `user_contact_notes`.
    notes: Mapped[str | None] = mapped_column(Text)
    # Provenance of the contact row — both "where we found the person" and "how
    # we got the email" collapsed into one field (the v0 admin-upload pattern
    # always produces the same value for both). Examples: linkedin-scrape,
    # 2026-iit-fair, referral-aman, manual-research, hunter-api. If a future
    # scraper needs to differentiate the two, re-split with a new migration.
    source: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("email", name="uq_contacts_email"),
        Index("idx_contacts_company", "company_id"),
    )
