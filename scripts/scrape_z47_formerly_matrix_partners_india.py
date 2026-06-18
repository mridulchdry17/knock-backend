"""Scrape Z47 (formerly Matrix Partners India) portfolio via static HTML.

For each portfolio company:
  - fetches the listing page to get all 78 slugs
  - fetches each /portfolio/{slug} detail page
  - extracts company name, description, sector, stage, year partnered
  - extracts founder full names with LinkedIn URLs
  - derives domain from company name (slug-based guess) for email construction
  - constructs firstname@domain for each founder (no real emails in HTML)
  - upserts Company + Contact into the pool

Usage:
    .venv/bin/python -m scripts.scrape_z47_formerly_matrix_partners_india

Idempotent — re-runs skip existing emails. Records the scraped_pattern as
"firstname" so the bounce-handler can try alternates (first.last, etc.) later.

Rate-limited to ~1 request/second to avoid bot detection.
"""
from __future__ import annotations

import re
import sys
import time
import urllib.parse
import urllib.request

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company, Contact

SOURCE_TAG = "z47-formerly-matrix-partners-india-scraping"
BASE_URL = "https://www.z47.com"
LISTING_URL = f"{BASE_URL}/portfolio"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Delay between requests in seconds
REQUEST_DELAY = 1.2


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _get_slugs(html: str) -> list[str]:
    """Extract all /portfolio/{slug} hrefs from the listing page."""
    matches = re.findall(r'href="/portfolio/([^"/]+)"', html)
    # Deduplicate while preserving order
    seen: set[str] = set()
    slugs: list[str] = []
    for s in matches:
        if s not in seen:
            seen.add(s)
            slugs.append(s)
    return slugs


def _parse_company_name(html: str, slug: str) -> str:
    """Extract company name from breadcrumb link, fallback to slug."""
    bc = re.findall(r'class="breadcrumb-link">([^<]+)<', html)
    # Last breadcrumb is the company name
    for item in reversed(bc):
        name = item.strip()
        if name and name.lower() != "portfolio":
            return name
    # fallback: capitalise slug
    return slug.replace("-", " ").title()


def _parse_description(html: str) -> str | None:
    """Extract one-line description from the portfolio hero content block."""
    # Description lives inside w-richtext > p within the company content section
    idx = html.find("portfolio-hero_company-content")
    if idx < 0:
        idx = html.find("portfolio-hero_content")
    if idx < 0:
        return None
    chunk = html[idx : idx + 2000]
    desc_matches = re.findall(r'w-richtext"><p>([^<]+)<', chunk)
    if desc_matches:
        return desc_matches[0].strip() or None
    return None


def _parse_sector(html: str) -> str | None:
    """Extract sector tag from the portfolio hero block."""
    idx = html.find("portfolio-hero")
    if idx < 0:
        return None
    chunk = html[idx : idx + 3000]
    # Prefer sector-tag that is NOT 'Software & AI' repeat — just take first unique
    sectors = re.findall(r'sector-tag[^"]*">([^<]+)<', chunk)
    # De-dup
    seen: set[str] = set()
    unique: list[str] = []
    for s in sectors:
        s = s.strip()
        s = s.replace("&amp;", "&")
        if s and s not in seen:
            seen.add(s)
            unique.append(s)
    return unique[0] if unique else None


def _parse_stage(html: str) -> str | None:
    """Extract funding stage."""
    m = re.search(r'is-funding[^"]*">([^<]+)<', html)
    return m.group(1).strip() if m else None


def _parse_year(html: str) -> str | None:
    """Extract year partnered."""
    m = re.search(r'company-year[^"]*">(\d{4})<', html)
    return m.group(1) if m else None


def _parse_founders(html: str) -> list[tuple[str, str | None]]:
    """Return list of (full_name, linkedin_url_or_None) for each founder."""
    idx = html.find("portfolio-hero_founders")
    if idx < 0:
        return []
    # Grab the founders section (up to investment team or a generous chunk)
    end_idx = html.find("portfolio-hero_investment", idx)
    if end_idx < 0:
        end_idx = html.find("investment-team", idx)
    if end_idx < 0:
        end_idx = idx + 5000
    section = html[idx:end_idx]

    # Each founder is an <a> with href to linkedin and a div with name
    results: list[tuple[str, str | None]] = []
    # Pattern: listitem blocks containing the founder anchor
    items = re.split(r'role="listitem"', section)
    for item in items[1:]:  # skip first split before any listitem
        linkedin_m = re.search(r'href="(https://www\.linkedin\.com/in/[^"]+)"', item)
        name_m = re.search(r'<div[^>]*text-size-xlarge[^>]*>([^<]+)<', item)
        if name_m:
            name = name_m.group(1).strip()
            linkedin = linkedin_m.group(1).rstrip("/") if linkedin_m else None
            results.append((name, linkedin))
    return results


def _slug_to_domain(company_name: str, slug: str) -> str:
    """
    Derive a plausible company domain from the company name or slug.
    Strategy: lowercase slug (hyphens removed) + .com
    E.g. "country-delight" -> "countrydelight.com"
    """
    clean = slug.lower().replace("-", "")
    return f"{clean}.com"


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


def main() -> int:
    print(f"Fetching Z47 portfolio listing from {LISTING_URL} ...")
    listing_html = _fetch(LISTING_URL)
    slugs = _get_slugs(listing_html)
    print(f"  Found {len(slugs)} portfolio slugs")

    counts = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_founders": 0,
        "skipped_bad_data": 0,
    }
    seen_emails: set[str] = set()

    db = SessionLocal()
    try:
        for i, slug in enumerate(slugs, 1):
            url = f"{BASE_URL}/portfolio/{slug}"
            print(f"[{i}/{len(slugs)}] Fetching {url} ...")
            try:
                html = _fetch(url)
            except Exception as exc:
                print(f"  ERROR fetching {url}: {exc}")
                counts["skipped_bad_data"] += 1
                time.sleep(REQUEST_DELAY)
                continue

            company_name = _parse_company_name(html, slug)
            description = _parse_description(html)
            sector = _parse_sector(html)
            stage = _parse_stage(html)
            year = _parse_year(html)
            founders = _parse_founders(html)

            # Derive domain from slug
            domain = _slug_to_domain(company_name, slug)

            print(f"  Name: {company_name} | Sector: {sector} | Stage: {stage} | "
                  f"Year: {year} | Domain: {domain} | Founders: {len(founders)}")

            # Upsert company by domain
            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    domain=domain,
                    name=company_name,
                    source=SOURCE_TAG,
                    funding_stage=stage,
                    industry=sector,
                    description=description,
                )
                db.add(company)
                db.flush()  # get company.id before inserting contacts
                counts["companies_created"] += 1
                print(f"  -> Company CREATED (id={company.id})")
            else:
                counts["companies_existing"] += 1
                print(f"  -> Company EXISTS (id={company.id})")

            if not founders:
                counts["skipped_no_founders"] += 1
                print("  -> No founders found, skipping contacts")
                time.sleep(REQUEST_DELAY)
                continue

            # One contact per founder
            for full_name, linkedin_url in founders:
                first = _first_name(full_name)
                if not first:
                    continue

                # No real emails in HTML — guess firstname@domain
                email = f"{first}@{domain}"

                if email in seen_emails:
                    counts["contacts_existing"] += 1
                    print(f"  -> Contact SKIPPED (in-run dup): {email}")
                    continue

                existing = db.scalar(select(Contact).where(Contact.email == email))
                if existing is not None:
                    counts["contacts_existing"] += 1
                    seen_emails.add(email)
                    print(f"  -> Contact EXISTS: {email}")
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
                    linkedin_url=linkedin_url,
                    source=SOURCE_TAG,
                )
                db.add(contact)
                counts["contacts_created"] += 1
                print(f"  -> Contact CREATED: {full_name} <{email}> | LinkedIn: {linkedin_url}")

            time.sleep(REQUEST_DELAY)

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
