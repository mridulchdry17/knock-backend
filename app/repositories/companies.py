from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.models import Company


def get_by_domain(db: OrmSession, domain: str) -> Company | None:
    return db.scalar(select(Company).where(Company.domain == domain))


def get_or_create_by_domain(
    db: OrmSession, *, domain: str, name: str, source: str = "admin-upload"
) -> Company:
    """Returns the company row for `domain`, creating one if missing.

    `name` is only used when creating — never overwrites an existing company's
    name (admin curation wins; bulk upload doesn't get to rename companies).
    """
    existing = get_by_domain(db, domain)
    if existing is not None:
        return existing

    company = Company(domain=domain, name=name, source=source)
    db.add(company)
    db.flush()  # populate company.id for FK use by callers in same txn
    return company
