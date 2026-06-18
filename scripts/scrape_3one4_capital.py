"""Scrape 3one4 Capital's portfolio via static HTML pages.

Two-step process:
  1. Fetch /portfolio to extract all company slugs from href links matching
     /portfolio-companies/SLUG pattern.
  2. Fetch each /portfolio-companies/SLUG detail page to extract:
       name, website, founders, description, and co-investors.

For each portfolio company:
  - derives domain from website URL
  - constructs firstname@domain for each founder (email_verified=False, confidence=60)
  - extracts real emails if present in HTML (email_verified=True, confidence=95)
  - extracts LinkedIn /in/ URLs for founders if present
  - upserts Company + Contact into the pool

Usage:
    .venv/bin/python -m scripts.scrape_3one4_capital

Idempotent — re-runs skip existing emails. Records scraped_pattern="firstname"
so the bounce-handler can try alternates (first.last, etc.) later.
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

BASE_URL = "https://www.3one4capital.com"
SOURCE_TAG = "3one4-capital-scraping"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Regex to find real email addresses embedded in HTML
REAL_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+\.[a-z]{2,}", re.IGNORECASE)


def _fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _fetch_slugs() -> list[str]:
    """Step 1: fetch /portfolio and extract all unique company slugs."""
    html = _fetch_html(f"{BASE_URL}/portfolio")
    slugs = re.findall(r'href="(/portfolio-companies/([^"/]+))"', html)
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for _full_path, slug in slugs:
        if slug not in seen:
            seen.add(slug)
            unique.append(slug)
    return unique


def _parse_domain(website: str) -> str | None:
    """Extract a clean domain from a (possibly UTM-laden) URL."""
    try:
        parsed = urllib.parse.urlparse(website)
        host = (parsed.netloc or parsed.path).lower().removeprefix("www.")
        return host if "." in host else None
    except Exception:
        return None


def _clean_url(website: str) -> str:
    """Strip query-string/fragment tracking params, keep scheme+host+path."""
    try:
        parsed = urllib.parse.urlparse(website)
        return urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, "", "", "")
        )
    except Exception:
        return website


def _parse_detail(slug: str) -> dict | None:
    """Step 2: fetch one portfolio-company detail page and return parsed data."""
    url = f"{BASE_URL}/portfolio-companies/{slug}"
    try:
        html = _fetch_html(url)
    except Exception as exc:
        print(f"    [WARN] Could not fetch {url}: {exc}")
        return None

    # Company name from <h1 class="h1">
    name_m = re.search(r'<h1 class="h1">(.*?)</h1>', html)
    name = name_m.group(1).strip() if name_m else slug.replace("-", " ").title()

    # Website: first https:// link inside pc-social-share-container
    website_m = re.search(
        r'class="pc-social-share-container".*?href="(https?://[^"]+)"',
        html,
        re.DOTALL,
    )
    website_raw = website_m.group(1) if website_m else None
    website = _clean_url(website_raw) if website_raw else None

    # Founders: all <div class="pc-funding-detail-text big">…</div> entries
    founders = re.findall(
        r'class="pc-funding-detail-text big">(.*?)</div>', html
    )
    founders = [f.strip() for f in founders if f.strip()]

    # Description: content inside pc-description w-richtext, strip tags
    desc_m = re.search(
        r'class="pc-description w-richtext">(.*?)</div>', html, re.DOTALL
    )
    description: str | None = None
    if desc_m:
        raw_desc = re.sub(r"<[^>]+>", " ", desc_m.group(1))
        description = " ".join(raw_desc.split()).strip() or None

    # Real emails found directly in HTML (rare but possible)
    real_emails = REAL_EMAIL_RE.findall(html)
    # Filter out obvious non-company emails (e.g. webflow system emails)
    real_emails = [
        e for e in real_emails
        if not any(
            skip in e.lower()
            for skip in ["@3one4capital", "@webflow", "@sentry", "@w3.org"]
        )
    ]

    # LinkedIn /in/ URLs (founder personal profiles)
    linkedin_urls = re.findall(
        r'href="(https://www\.linkedin\.com/in/[^"]+)"', html
    )
    linkedin_urls = list(dict.fromkeys(linkedin_urls))  # deduplicate

    return {
        "name": name,
        "website": website,
        "founders": founders,
        "description": description,
        "real_emails": real_emails,
        "linkedin_urls": linkedin_urls,
    }


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


def main() -> int:
    print("Step 1: Fetching 3one4 Capital portfolio page for company slugs...")
    slugs = _fetch_slugs()
    print(f"  Found {len(slugs)} unique company slugs")

    counts = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_website": 0,
        "skipped_bad_domain": 0,
        "skipped_no_founders": 0,
    }
    seen_emails: set[str] = set()

    db = SessionLocal()
    try:
        print("Step 2: Scraping each company detail page...")
        for i, slug in enumerate(slugs, 1):
            print(f"  [{i}/{len(slugs)}] {slug}")
            detail = _parse_detail(slug)
            if detail is None:
                continue

            # Rate-limit — be polite to the server
            time.sleep(0.5)

            website = detail["website"]
            if not website:
                print(f"    -> Skipped: no website")
                counts["skipped_no_website"] += 1
                continue

            domain = _parse_domain(website)
            if not domain:
                print(f"    -> Skipped: bad domain from '{website}'")
                counts["skipped_bad_domain"] += 1
                continue

            # Upsert company by domain
            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    domain=domain,
                    name=detail["name"],
                    source=SOURCE_TAG,
                    funding_stage=None,   # not available on page
                    industry=None,        # not available on page
                    description=detail["description"],
                )
                db.add(company)
                db.flush()  # get company.id before inserting contacts
                counts["companies_created"] += 1
                print(f"    -> Company created: {detail['name']} ({domain})")
            else:
                counts["companies_existing"] += 1
                print(f"    -> Company existing: {detail['name']} ({domain})")

            founders = detail["founders"]
            if not founders:
                print(f"    -> No founders found, skipping contacts")
                counts["skipped_no_founders"] += 1
                continue

            real_emails: list[str] = detail["real_emails"]
            linkedin_urls: list[str] = detail["linkedin_urls"]

            for idx, full_name in enumerate(founders):
                first = _first_name(full_name)
                if not first:
                    continue

                # Check for a real email matching this founder (first try)
                founder_real_email: str | None = None
                for re_email in real_emails:
                    if re_email.lower().startswith(first):
                        founder_real_email = re_email.lower()
                        break

                if founder_real_email:
                    email = founder_real_email
                    email_verified = True
                    email_confidence = 95
                    scraped_pattern = None
                else:
                    email = f"{first}@{domain}"
                    email_verified = False
                    email_confidence = 60
                    scraped_pattern = "firstname"

                # Assign LinkedIn URL by position if available
                linkedin_url: str | None = None
                if idx < len(linkedin_urls):
                    linkedin_url = linkedin_urls[idx]

                if email in seen_emails:
                    counts["contacts_existing"] += 1
                    continue
                existing = db.scalar(
                    select(Contact).where(Contact.email == email)
                )
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
                    linkedin_url=linkedin_url,
                    source=SOURCE_TAG,
                )
                db.add(contact)
                counts["contacts_created"] += 1
                print(
                    f"    -> Contact: {full_name} <{email}>"
                    + (" [real]" if founder_real_email else " [guessed]")
                )

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
