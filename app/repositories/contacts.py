from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as OrmSession

from app.core.emails import normalize_email
from app.models import Company, Contact


def get(db: OrmSession, contact_id: int) -> Contact | None:
    return db.get(Contact, contact_id)


def get_by_email(db: OrmSession, email: str) -> Contact | None:
    return db.scalar(select(Contact).where(Contact.email == normalize_email(email)))


def get_by_emails(db: OrmSession, emails: Iterable[str]) -> dict[str, Contact]:
    """Bulk lookup keyed by normalized email. Used by bulk_upsert to dedup in
    a single query instead of N+1."""
    normed = {normalize_email(e) for e in emails if e}
    if not normed:
        return {}
    rows = db.scalars(select(Contact).where(Contact.email.in_(normed))).all()
    return {c.email: c for c in rows if c.email is not None}


def add(db: OrmSession, contact: Contact) -> Contact:
    db.add(contact)
    db.flush()
    return contact


def delete_by_id(db: OrmSession, contact_id: int) -> None:
    contact = db.get(Contact, contact_id)
    if contact is not None:
        db.delete(contact)


def list_admin_paginated(
    db: OrmSession,
    *,
    search: str | None = None,
    company_domain: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[tuple[Contact, Company]], int]:
    """Returns ((contact, company) pairs, total).

    Join is one trip; admin listing always wants the company name/domain
    alongside each contact row.
    """
    base = select(Contact, Company).join(Company, Contact.company_id == Company.id)
    count_base = select(func.count()).select_from(Contact).join(
        Company, Contact.company_id == Company.id
    )

    if company_domain:
        base = base.where(Company.domain == company_domain.lower().strip())
        count_base = count_base.where(Company.domain == company_domain.lower().strip())

    if search:
        needle = f"%{search.lower()}%"
        cond = or_(
            func.lower(Contact.email).like(needle),
            func.lower(Contact.name).like(needle),
            func.lower(Company.name).like(needle),
        )
        base = base.where(cond)
        count_base = count_base.where(cond)

    rows = list(
        db.execute(
            base.order_by(Contact.created_at.desc()).limit(limit).offset(offset)
        ).all()
    )
    pairs = [(r[0], r[1]) for r in rows]
    total = db.scalar(count_base) or 0
    return pairs, total


def count_by_company(db: OrmSession) -> list[tuple[Company, int]]:
    """Aggregated contact counts per company. Sorted by count desc, then domain."""
    stmt = (
        select(Company, func.count(Contact.id).label("n"))
        .join(Contact, Contact.company_id == Company.id)
        .group_by(Company.id)
        .order_by(func.count(Contact.id).desc(), Company.domain.asc())
    )
    return [(row[0], int(row[1])) for row in db.execute(stmt).all()]
