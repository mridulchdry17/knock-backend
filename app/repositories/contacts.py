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


def list_browse_paginated(
    db: OrmSession,
    *,
    exclude_domains: set[str],
    search: str | None,
    company_domain: str | None,
    limit: int,
    offset: int,
) -> tuple[list[tuple[Contact, Company]], int]:
    """User-facing browse listing.

    Hard filters applied at the SQL layer:
      - `Company.domain NOT IN exclude_domains` (user's excluded set +
        platform-permanent locks + user's own reply locks — caller pre-merges
        these into one set so we run a single round-trip)
      - `Contact.is_invalid = False` — invalid addresses don't belong in browse
      - optional `company_domain` and `search` filters

    Note: the 36h platform cooldown is intentionally NOT enforced here. Cooldown
    state is surfaced per-row via `availability` so the user can see "available
    in 12h" rather than have contacts vanish from their list temporarily.
    """
    base = (
        select(Contact, Company)
        .join(Company, Contact.company_id == Company.id)
        .where(Contact.is_invalid.is_(False))
    )
    count_base = (
        select(func.count())
        .select_from(Contact)
        .join(Company, Contact.company_id == Company.id)
        .where(Contact.is_invalid.is_(False))
    )

    if exclude_domains:
        base = base.where(~Company.domain.in_(exclude_domains))
        count_base = count_base.where(~Company.domain.in_(exclude_domains))

    if company_domain:
        normed = company_domain.lower().strip()
        base = base.where(Company.domain == normed)
        count_base = count_base.where(Company.domain == normed)

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
            base.order_by(Company.name.asc(), Contact.id.asc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
    pairs = [(r[0], r[1]) for r in rows]
    total = db.scalar(count_base) or 0
    return pairs, total


def get_with_company(
    db: OrmSession, contact_id: int
) -> tuple[Contact, Company] | None:
    """Single contact + its company in one query (browse-detail use)."""
    row = db.execute(
        select(Contact, Company)
        .join(Company, Contact.company_id == Company.id)
        .where(Contact.id == contact_id)
    ).first()
    if row is None:
        return None
    return row[0], row[1]


def count_by_company(db: OrmSession) -> list[tuple[Company, int]]:
    """Aggregated contact counts per company. Sorted by count desc, then domain."""
    stmt = (
        select(Company, func.count(Contact.id).label("n"))
        .join(Contact, Contact.company_id == Company.id)
        .group_by(Company.id)
        .order_by(func.count(Contact.id).desc(), Company.domain.asc())
    )
    return [(row[0], int(row[1])) for row in db.execute(stmt).all()]
