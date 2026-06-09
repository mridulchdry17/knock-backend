from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base
from app.models._mixins import CreatedAtMixin


class Template(Base, CreatedAtMixin):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(String, nullable=False)
    is_followup: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parent_template_id: Mapped[int | None] = mapped_column(ForeignKey("templates.id"))
    # True for the 3 templates seeded on first login. Lets the UI badge them and
    # distinguishes seeded vs user-authored.
    is_starter: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Nullable in the migration for any legacy rows; the model sets + bumps it.
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
