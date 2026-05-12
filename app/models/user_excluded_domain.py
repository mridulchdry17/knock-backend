from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models._mixins import CreatedAtMixin


class UserExcludedDomain(Base, CreatedAtMixin):
    """Per-user excluded recipient domains.

    The ONE preference that drives v0 batch-picker behavior: the picker (B5.4)
    filters contact candidates whose email domain matches any row in this
    table for the requesting user.

    Composite PK (user_id, domain) gives free uniqueness enforcement —
    re-inserting the same domain raises IntegrityError, which the repository
    surfaces as `was_created=False` rather than 500.
    """

    __tablename__ = "user_excluded_domains"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    domain: Mapped[str] = mapped_column(String(255), primary_key=True)

    __table_args__ = (Index("ix_user_excluded_domains_domain", "domain"),)
