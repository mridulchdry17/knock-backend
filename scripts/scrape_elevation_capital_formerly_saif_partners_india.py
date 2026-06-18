"""Scrape Elevation Capital (formerly SAIF Partners India) portfolio via public Sanity CMS API.

For each portfolio company:
  - fetches title, founders, website, stage, short_description, bio, inception
  - derives domain from website URL
  - scans text fields for real email addresses (email_verified=True, confidence=95)
  - falls back to firstname@domain guess (email_verified=False, confidence=60)
  - extracts LinkedIn URLs from links field if present
  - upserts Company + Contact into the pool

Usage:
    .venv/bin/python -m scripts.scrape_elevation_capital_formerly_saif_partners_india

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
SOURCE_TAG = "elevation-capital-formerly-saif-partners-india-scraping"

GROQ_QUERY = """*[_type == "portfolioCompany" && visible == true]{
  title,
  website,
  founders,
  stage,
  short_description,
  bio,
  inception
}"""

EMAIL_RE = re.compile(r'[\w.+\-]+@[\w\-]+\.[a-z]{2,}', re.IGNORECASE)
LINKEDIN_RE = re.compile(r'https?://(?:www\.)?linkedin\.com/in/[^\s"\'<>,]+', re.IGNORECASE)


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
    if re.fullmatch(r"[-–—N/Atbd.]+", founders_raw, re.IGNORECASE):
        return []
    # normalise separators: newlines, " and ", semicolons → comma
    cleaned = re.sub(r"\s+and\s+", ", ", founders_raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"[\n;]+", ", ", cleaned)
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    return parts


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


def _scan_text_for_emails(record: dict) -> list[str]:
    """Return any real email addresses found in any text field of the record."""
    blob = " ".join(
        str(v) for v in [
            record.get("bio", ""),
            record.get("short_description", ""),
            record.get("founders", ""),
        ]
        if v
    )
    return EMAIL_RE.findall(blob)


def _scan_for_linkedin(record: dict) -> str | None:
    """Return first LinkedIn profile URL found in the record's text/links fields."""
    # Check links field (may be a list of dicts or strings)
    links = record.get("links") or []
    if isinstance(links, list):
        for item in links:
            if isinstance(item, dict):
                href = item.get("href") or item.get("url") or ""
            else:
                href = str(item)
            m = LINKEDIN_RE.search(href)
            if m:
                return m.group(0)
    elif isinstance(links, str):
        m = LINKEDIN_RE.search(links)
        if m:
            return m.group(0)

    # Also scan text fields
    blob = " ".join(
        str(v) for v in [
            record.get("bio", ""),
            record.get("short_description", ""),
        ]
        if v
    )
    m = LINKEDIN_RE.search(blob)
    return m.group(0) if m else None


def main() -> int:
    print("Fetching Elevation Capital (formerly SAIF Partners India) portfolio from Sanity API...")
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

            if not website:
                counts["skipped_no_website"] += 1
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
                    description=description,
                )
                db.add(company)
                db.flush()
                counts["companies_created"] += 1
            else:
                counts["companies_existing"] += 1

            if not founders_raw:
                counts["skipped_no_founders"] += 1
                continue

            # Check for real emails in record text fields first.
            real_emails = _scan_text_for_emails(record)
            linkedin_url = _scan_for_linkedin(record)

            founder_names = _parse_founders(founders_raw)

            for i, full_name in enumerate(founder_names):
                first = _first_name(full_name)
                if not first:
                    continue

                # Priority 1: real email found in text fields (assign to first founder,
                # or match by name heuristic if multiple)
                real_email_for_founder: str | None = None
                if real_emails:
                    # Try to find an email that starts with this founder's first name
                    for em in real_emails:
                        local = em.split("@")[0].lower()
                        if local.startswith(first):
                            real_email_for_founder = em.lower()
                            break
                    # If no name-matched email, assign the first real email to the first founder only
                    if real_email_for_founder is None and i == 0:
                        real_email_for_founder = real_emails[0].lower()

                if real_email_for_founder:
                    email = real_email_for_founder
                    email_verified = True
                    email_confidence = 95
                    scraped_pattern = None
                else:
                    email = f"{first}@{domain}"
                    email_verified = False
                    email_confidence = 60
                    scraped_pattern = "firstname"

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
                    email_verified=email_verified,
                    email_confidence=email_confidence,
                    scraped_pattern=scraped_pattern,
                    source=SOURCE_TAG,
                )
                # Attach LinkedIn URL if the model supports it and we found one
                if linkedin_url and hasattr(contact, "linkedin_url"):
                    contact.linkedin_url = linkedin_url
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
