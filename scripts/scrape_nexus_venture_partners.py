"""Scrape Nexus Venture Partners' portfolio via the public WP REST API.

For each portfolio company:
  - fetches title, slug, text description, sector categories, location tags,
    and portfolio_tag (Nexus partner / investor names stored as taxonomy)
  - company website URLs are NOT available in the REST API (ACF fields are
    empty); we construct a placeholder domain from the company slug so that
    the Company row can still be upserted
  - because no real founder names or emails are returned by the API, contacts
    are skipped (no guessable firstname@domain without a real domain)

Pagination: page 1 returns up to 100 entries, page 2 returns the remainder.

Usage:
    .venv/bin/python -m scripts.scrape_nexus_venture_partners

Idempotent — re-runs skip existing companies.
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

SOURCE_TAG = "nexus-venture-partners-scraping"
BASE_URL = "https://nexusvp.com/wp-json/wp/v2"

EMAIL_RE = re.compile(r"[w.+\-]+@[w\-]+\.[a-z]{2,}", re.IGNORECASE)
LINKEDIN_RE = re.compile(r'href=["\']([^"\']*linkedin\.com/in/[^"\']+)["\']', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def _fetch_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; scraper/1.0)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _fetch_all_portfolio() -> list[dict]:
    """Fetch all portfolio entries across pages."""
    records: list[dict] = []
    page = 1
    while True:
        url = f"{BASE_URL}/portfolio?per_page=100&page={page}"
        print(f"  Fetching portfolio page {page} → {url}")
        try:
            batch = _fetch_json(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                # WP returns 400 when page is beyond last page
                break
            raise
        if not isinstance(batch, list) or len(batch) == 0:
            break
        records.extend(batch)
        print(f"    Got {len(batch)} records (total so far: {len(records)})")
        if len(batch) < 100:
            break
        page += 1
    return records


def _fetch_term_names(taxonomy: str, ids: list[int]) -> list[str]:
    """Resolve taxonomy term IDs to names (single API call per id)."""
    names: list[str] = []
    for tid in ids:
        try:
            url = f"{BASE_URL}/{taxonomy}/{tid}"
            data = _fetch_json(url)
            name = (data.get("name") or "").strip()
            if name:
                names.append(name)
        except Exception:
            pass
    return names


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _slug_to_domain(slug: str) -> str:
    """Convert a WP slug like 'some-company' to 'some-company.com'."""
    clean = slug.strip().lower()
    return f"{clean}.com"


def _parse_description(record: dict) -> str | None:
    """Extract plain-text description from rendered content or excerpt."""
    content = (record.get("content") or {}).get("rendered") or ""
    excerpt = (record.get("excerpt") or {}).get("rendered") or ""
    raw = content or excerpt
    if not raw:
        return None
    # Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _extract_real_emails(text: str) -> list[str]:
    return EMAIL_RE.findall(text or "")


def _extract_linkedin_urls(html: str) -> list[str]:
    return LINKEDIN_RE.findall(html or "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Fetching Nexus Venture Partners portfolio from WP REST API...")
    portfolio_records = _fetch_all_portfolio()
    print(f"  Total portfolio records: {len(portfolio_records)}")

    counts = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_title": 0,
        "skipped_no_slug": 0,
    }
    seen_emails: set[str] = set()

    db = SessionLocal()
    try:
        for record in portfolio_records:
            title = (record.get("title") or {}).get("rendered") or ""
            title = re.sub(r"<[^>]+>", "", title).strip()
            slug = (record.get("slug") or "").strip()

            if not title:
                counts["skipped_no_title"] += 1
                continue
            if not slug:
                counts["skipped_no_slug"] += 1
                continue

            description = _parse_description(record)

            # Sector from portfolio_category taxonomy IDs
            sector_ids: list[int] = record.get("portfolio_category") or []
            sector: str | None = None
            if sector_ids:
                sector_names = _fetch_term_names("portfolio_category", sector_ids[:1])
                sector = sector_names[0] if sector_names else None

            # Location from portfolio_location taxonomy IDs
            location_ids: list[int] = record.get("portfolio_location") or []
            location: str | None = None
            if location_ids:
                loc_names = _fetch_term_names("portfolio_location", location_ids[:1])
                location = loc_names[0] if loc_names else None

            # Partner names from portfolio_tag taxonomy (Nexus partner/investor names)
            partner_tag_ids: list[int] = record.get("portfolio_tag") or []
            # We don't map partners to contacts — just note for potential future use.

            # Build a placeholder domain from slug (real domains not in API)
            domain = _slug_to_domain(slug)

            # Upsert company by domain
            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    domain=domain,
                    name=title,
                    source=SOURCE_TAG,
                    industry=sector,
                    description=description,
                )
                db.add(company)
                db.flush()
                counts["companies_created"] += 1
            else:
                counts["companies_existing"] += 1

            # Scan raw content for any real emails and LinkedIn URLs
            raw_content = (record.get("content") or {}).get("rendered") or ""
            raw_excerpt = (record.get("excerpt") or {}).get("rendered") or ""
            combined_html = raw_content + " " + raw_excerpt

            real_emails = _extract_real_emails(combined_html)
            linkedin_urls = _extract_linkedin_urls(combined_html)

            # If real emails found, create contacts for them
            for email in real_emails:
                email = email.lower().strip()
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
                    name=title,  # no founder name available; use company name
                    email=email,
                    role="Founder",
                    email_verified=True,
                    email_confidence=95,
                    scraped_pattern=None,
                    source=SOURCE_TAG,
                    linkedin_url=linkedin_urls[0] if linkedin_urls else None,
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
