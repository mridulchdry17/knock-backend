"""Scrape Accel India's portfolio via their public Sanity CMS API.

For each portfolio company (India & SEA region):
  - fetches name, founders, websiteUrl, headquarters, shortDescription
  - filters to India & SEA region using the Sanity region reference ID
  - derives domain from websiteUrl
  - parses Portable Text block arrays for founder names
  - constructs firstname@domain for each founder
  - upserts Company + Contact into the pool

Usage:
    .venv/bin/python -m scripts.scrape_accel_india

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

SANITY_PROJECT = "458oembh"
SANITY_DATASET = "production"
SOURCE_TAG = "accel-india-scraping"

# Sanity region document _id for "India & SEA"
INDIA_SEA_REGION_ID = "e3ed3088-d45f-4d90-9ee2-481c964ff209"

GROQ_QUERY = (
    f'*[_type == "company" && region._ref == "{INDIA_SEA_REGION_ID}"]'
    '{name, websiteUrl, shortDescription, founders, headquarters, location,'
    ' initialInvestmentType, currentStatus, archived}'
)


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
        # strip trailing paths
        host = host.split("/")[0]
        return host if "." in host else None
    except Exception:
        return None


def _portable_text_to_string(blocks) -> str:
    """Extract plain text from a Sanity Portable Text block array."""
    if not blocks or not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("_type", "")
        if block_type == "block":
            for child in block.get("children", []):
                if isinstance(child, dict) and child.get("_type") == "span":
                    text = child.get("text") or ""
                    if text:
                        parts.append(text)
        elif block_type == "span":
            text = block.get("text") or ""
            if text:
                parts.append(text)
        elif isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _parse_founders(founders_raw) -> list[str]:
    """Return a list of individual founder full names.

    founders_raw may be:
      - a Portable Text block array  (Sanity's rich text)
      - a plain string
      - a list of strings
      - None / empty
    """
    if not founders_raw:
        return []

    # Portable Text: list of block objects
    if isinstance(founders_raw, list):
        if founders_raw and isinstance(founders_raw[0], dict):
            text = _portable_text_to_string(founders_raw)
        else:
            # plain list of strings
            text = ", ".join(str(i) for i in founders_raw if i)
    else:
        text = str(founders_raw)

    text = text.strip()
    if not text:
        return []

    # Placeholder entries like "-", "N/A", "TBD"
    if re.fullmatch(r"[-–—N/Atbd.]+", text, re.IGNORECASE):
        return []

    # Normalise separators: newlines, " and ", semicolons → comma
    cleaned = re.sub(r"\s+and\s+", ", ", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"[\n;]+", ", ", cleaned)
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    return parts


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


def _extract_real_emails(record: dict) -> list[str]:
    """Scan all string fields in the record for real email addresses."""
    email_re = re.compile(r'[\w.+\-]+@[\w\-]+\.[a-z]{2,}', re.IGNORECASE)
    found: list[str] = []

    def _scan(val):
        if isinstance(val, str):
            found.extend(email_re.findall(val))
        elif isinstance(val, list):
            for item in val:
                _scan(item)
        elif isinstance(val, dict):
            for v in val.values():
                _scan(v)

    for v in record.values():
        _scan(v)

    return list(set(e.lower() for e in found))


def main() -> int:
    print("Fetching Accel India & SEA portfolio from Sanity API...")
    companies = _fetch_portfolio()
    print(f"  {len(companies)} India & SEA records returned")

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
    seen_domains: dict[str, Company] = {}  # domain -> Company, for in-run dedup

    db = SessionLocal()
    try:
        for record in companies:
            name = (record.get("name") or "").strip()
            founders_raw = record.get("founders")
            website = (record.get("websiteUrl") or "").strip()
            # shortDescription may be a Portable Text block array
            desc_raw = record.get("shortDescription") or ""
            if isinstance(desc_raw, list):
                desc_raw = _portable_text_to_string(desc_raw)
            description = desc_raw.strip() or None
            stage = record.get("initialInvestmentType") or None

            if not website:
                counts["skipped_no_website"] += 1
                continue

            domain = _parse_domain(website)
            if not domain:
                counts["skipped_bad_domain"] += 1
                continue

            company_name = name or domain

            # Upsert company by domain — check in-run cache first to avoid
            # unique-constraint violations when the same domain appears twice
            # in the Sanity dataset before we've committed.
            if domain in seen_domains:
                company = seen_domains[domain]
                counts["companies_existing"] += 1
            else:
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
                    db.flush()   # get company.id before inserting contacts
                    counts["companies_created"] += 1
                else:
                    counts["companies_existing"] += 1
                seen_domains[domain] = company

            # --- Contacts ---

            # Priority 1: real emails found anywhere in the record fields
            real_emails = _extract_real_emails(record)
            for email in real_emails:
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
                    email=email,
                    role="Founder",
                    email_verified=True,
                    email_confidence=95,
                    scraped_pattern=None,
                    source=SOURCE_TAG,
                )
                db.add(contact)
                counts["contacts_created"] += 1

            # Priority 2: guess firstname@domain from founder names
            founders = _parse_founders(founders_raw)
            if not founders:
                counts["skipped_no_founders"] += 1
                # Still keep the company — just no contacts
            else:
                for full_name in founders:
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
