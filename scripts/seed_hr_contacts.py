"""One-shot seed: ingest the curated HR-contact CSV into the live pool.

Usage:
    .venv/bin/python -m scripts.seed_hr_contacts

Reads data/hr_contacts.csv, derives `company_domain` from each email, upserts a
Company per unique domain and a Contact per email, tags everything with
source='hr_seed_2026-06-11' so the batch can be filtered/deleted later from
the admin Contact Pool page.

Fully idempotent — re-runs skip existing emails. Safe to run multiple times.

Skips junk rows: undefined placeholders, role-mailboxes (info@, hr@, jobs@,
qa@, solution.head@, etc.), and anything missing email/name. Counts the skips
so the operator sees what landed vs what didn't.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company, Contact

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "hr_contacts.csv"
SOURCE_TAG = "hr_seed_2026-06-11"

# Local-parts we don't want — they're shared mailboxes, not real people.
ROLE_MAILBOX_LOCALPARTS = {
    "info", "hr", "hrd", "jobs", "qa", "solution.head", "support",
    "contact", "careers", "talent",
}

# Specific emails to skip (placeholder / test entries we know about).
SKIP_EMAILS = {
    "undefined",
    "friend.friend@niit-tech.com",
    "inventum.recruiter@inventum.net",
    "jpixentia@pixentia.com",
}


def _is_junk(name: str, email: str) -> tuple[bool, str | None]:
    """Return (is_junk, reason) — reason is None when the row is clean."""
    if not name or not email or "@" not in email:
        return True, "missing_name_or_email"
    if "undefined" in name.lower() or email in SKIP_EMAILS:
        return True, "placeholder"
    local = email.split("@", 1)[0]
    if local in ROLE_MAILBOX_LOCALPARTS:
        return True, "role_mailbox"
    return False, None


def main() -> int:
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found", file=sys.stderr)
        return 1

    with open(CSV_PATH) as f:
        rows = list(csv.DictReader(f))
    print(f"Read {len(rows)} rows from {CSV_PATH.name}")

    counts = {
        "companies_created": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_junk": 0,
        "skipped_duplicate_in_file": 0,
    }
    seen_emails_this_run: set[str] = set()

    db = SessionLocal()
    try:
        for row in rows:
            name = (row.get("Name") or "").strip()
            email = (row.get("Email") or "").strip().lower()
            title = (row.get("Title") or "").strip()
            company_name = (row.get("Company") or "").strip()

            junk, _ = _is_junk(name, email)
            if junk:
                counts["skipped_junk"] += 1
                continue
            if email in seen_emails_this_run:
                counts["skipped_duplicate_in_file"] += 1
                continue
            seen_emails_this_run.add(email)

            domain = email.split("@", 1)[1].lower()

            # Upsert company by domain.
            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    domain=domain,
                    name=company_name or domain,
                    source=SOURCE_TAG,
                )
                db.add(company)
                db.flush()
                counts["companies_created"] += 1

            # Skip if a contact with this email already exists.
            existing = db.scalar(select(Contact).where(Contact.email == email))
            if existing is not None:
                counts["contacts_existing"] += 1
                continue

            contact = Contact(
                company_id=company.id,
                email=email,
                name=name or None,
                role=title or None,
                source=SOURCE_TAG,
            )
            db.add(contact)
            counts["contacts_created"] += 1

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print("\n=== Seed complete ===")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
