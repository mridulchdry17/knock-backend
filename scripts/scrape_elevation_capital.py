"""Scrape Elevation Capital's portfolio via their public Sanity CMS API.

For each portfolio company:
  - fetches title, founders, website, stage, industry
  - derives domain from website URL
  - constructs firstname@domain for each founder
  - upserts Company + Contact into the pool

Usage:
    .venv/bin/python -m scripts.scrape_elevation_capital

Idempotent — re-runs skip existing emails. Records the scraped_pattern as
"firstname" so the bounce-handler can try alternates (first.last, etc.) later.
"""
from __future__ import annotations

import re
import sys
import urllib.parse
import urllib.request
import json

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company, Contact

SANITY_PROJECT = "gxmub2ol"
SANITY_DATASET = "redesign-2024"
SOURCE_TAG = "elevation-capital-scraping"

GROQ_QUERY = """*[_type == "portfolioCompany" && visible == true]{
  title,
  founders,
  website,
  stage,
  short_description,
  "industry": tag->name
}"""


def _fetch_portfolio() -> list[dict]:
    encoded = urllib.parse.quote(GROQ_QUERY)
    url = (
        f"https://{SANITY_PROJECT}.api.sanity.io"
        f"/v2021-10-21/data/query/{SANITY_DATASET}"
        f"?query={encoded}"
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())
    return data.get("result", [])


def _parse_domain(website: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(website)
        host = parsed.netloc or parsed.path
        host = host.lower().removeprefix("www.")
        return host if "." in host else None
    except Exception:
        return None


def _parse_founders(founders_raw: str) -> list[str]:
    """Split comma/newline/and-separated founder strings into individual full names."""
    # placeholder entries like "-", "N/A", "TBD"
    if re.fullmatch(r"[-–—N/Atbd.]+", founders_raw, re.IGNORECASE):
        return []
    # normalise separators: newlines, " and ", semicolons → comma
    cleaned = re.sub(r"\s+and\s+", ", ", founders_raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"[\n;]+", ", ", cleaned)
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    return parts


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


def main() -> int:
    print("Fetching Elevation Capital portfolio from Sanity API...")
    companies = _fetch_portfolio()
    print(f"  {len(companies)} records returned")

    counts = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_website": 0,
        "skipped_no_founders": 0,
        "skipped_bad_domain": 0,
    }
    seen_emails: set[str] = set()

    db = SessionLocal()
    try:
        for record in companies:
            title = (record.get("title") or "").strip()
            founders_raw = (record.get("founders") or "").strip()
            website = (record.get("website") or "").strip()
            stage = record.get("stage") or None
            description = (record.get("short_description") or "").strip() or None
            industry = record.get("industry") or None

            if not website:
                counts["skipped_no_website"] += 1
                continue

            if not founders_raw:
                counts["skipped_no_founders"] += 1
                continue

            domain = _parse_domain(website)
            if not domain:
                counts["skipped_bad_domain"] += 1
                continue

            company_name = title or domain

            # Upsert company by domain.
            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    domain=domain,
                    name=company_name,
                    source=SOURCE_TAG,
                    funding_stage=stage,
                    industry=industry,
                    description=description,
                )
                db.add(company)
                db.flush()
                counts["companies_created"] += 1
            else:
                counts["companies_existing"] += 1

            # One contact per founder.
            for full_name in _parse_founders(founders_raw):
                first = _first_name(full_name)
                if not first:
                    continue
                email = f"{first}@{domain}"

                if email in seen_emails:
                    counts["contacts_existing"] += 1
                    continue
                existing = db.scalar(select(Contact).where(Contact.email == email))
                if existing is not None:
                    counts["contacts_existing"] += 1
                    continue
                seen_emails.add(email)

                contact = Contact(
                    company_id=company.id,
                    name=full_name,
                    email=email,
                    role="Founder",
                    email_verified=False,
                    email_confidence=60,
                    scraped_pattern="firstname",
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

    print("\n=== Scrape complete ===")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
