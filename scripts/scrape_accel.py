"""Scrape Accel's global portfolio via their public Sanity CMS API.

Accel differs from Elevation Capital in two ways:
  - founders field is Portable Text (nested blocks), not a plain string
  - website field is called websiteUrl, not website
  - 783 companies across US, India, Europe

Usage:
    .venv/bin/python -m scripts.scrape_accel
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company, Contact

SANITY_PROJECT = "458oembh"
SANITY_DATASET = "production"
SOURCE_TAG = "accel-scraping"

PAGE_SIZE = 200


def _build_query(offset: int, page_size: int) -> str:
    end = offset + page_size
    return (
        f'*[_type == "company" && archived != true][{offset}..{end}]'
        '{name, websiteUrl, founders, initialInvestmentType, shortDescription, headquarters}'
    )


def _fetch_portfolio() -> list[dict]:
    all_records: list[dict] = []
    offset = 0
    while True:
        query = _build_query(offset, PAGE_SIZE)
        encoded = urllib.parse.quote(query)
        url = (
            f"https://{SANITY_PROJECT}.api.sanity.io"
            f"/v2021-10-21/data/query/{SANITY_DATASET}"
            f"?query={encoded}"
        )
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read())
        batch = data.get("result", [])
        all_records.extend(batch)
        print(f"  fetched {len(all_records)} so far...")
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return all_records


def _extract_portable_text(blocks: list | None) -> str:
    """Extract plain text from Sanity Portable Text blocks."""
    if not blocks:
        return ""
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for child in block.get("children", []):
            text = child.get("text", "")
            if text:
                parts.append(text)
    return " ".join(parts)


def _parse_domain(website: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(website)
        host = (parsed.netloc or parsed.path).lower().removeprefix("www.")
        return host if "." in host else None
    except Exception:
        return None


def _parse_founders(founders_raw: str) -> list[str]:
    if re.fullmatch(r"[-–—N/Atbd.]+", founders_raw, re.IGNORECASE):
        return []
    cleaned = re.sub(r"\s+and\s+", ", ", founders_raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"[\n;]+", ", ", cleaned)
    return [p.strip() for p in cleaned.split(",") if p.strip()]


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


def main() -> int:
    print("Fetching Accel portfolio from Sanity API...")
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
            company_name = (record.get("name") or "").strip()
            website = (record.get("websiteUrl") or "").strip()
            founders_raw = _extract_portable_text(record.get("founders"))
            stage = record.get("initialInvestmentType") or None
            description = _extract_portable_text(record.get("shortDescription")) or None
            headquarters = (record.get("headquarters") or "").strip() or None

            if not website:
                counts["skipped_no_website"] += 1
                continue

            domain = _parse_domain(website)
            if not domain:
                counts["skipped_bad_domain"] += 1
                continue

            if not founders_raw:
                counts["skipped_no_founders"] += 1
                continue

            name = company_name or domain

            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    domain=domain,
                    name=name,
                    source=SOURCE_TAG,
                    funding_stage=stage,
                    description=description,
                    industry=headquarters,
                )
                db.add(company)
                db.flush()
                counts["companies_created"] += 1
            else:
                counts["companies_existing"] += 1

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
