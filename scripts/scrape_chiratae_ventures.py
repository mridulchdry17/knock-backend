"""Scrape Chiratae Ventures' portfolio via their public static HTML page.

For each portfolio company card:
  - extracts company name, website URL, founding year, location, sector tags, description
  - extracts LinkedIn URL from social media icons
  - derives domain from website URL
  - since no founder names are present on the page, creates Company rows only
    (no Contact rows — there are no emails or founder names to work with)

Usage:
    .venv/bin/python -m scripts.scrape_chiratae_ventures

Idempotent — re-runs skip existing domains.  Deduplicates companies that
appear in multiple category tabs by tracking seen URLs/domains in-run.

WordPress site: all data is rendered in static HTML — no JS rendering needed.
82+ companies found across category tabs (deduplication required).
"""
from __future__ import annotations

import re
import sys
import urllib.parse
import urllib.request

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company, Contact

PORTFOLIO_URL = "https://www.chiratae.com/companies/"
SOURCE_TAG = "chiratae-ventures-scraping"

# User-agent to avoid bot-blocking
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def _fetch_html() -> str:
    req = urllib.request.Request(PORTFOLIO_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_domain(website: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(website)
        host = parsed.netloc or parsed.path
        host = host.lower().removeprefix("www.")
        return host if "." in host else None
    except Exception:
        return None


def _extract_cards(html: str) -> list[dict]:
    """Parse all company cards from the static HTML.

    Each card has:
      name        - h2.card-title text
      tags        - list of p.company-tag-item texts (year, location, sector tags)
      description - div.card-description1 text
      website_url - a.company-link-item href
      linkedin_url - social icon href containing linkedin.com
      social_urls  - all social href values
    """
    # Match full card blocks: from card-item through the closing social-media-icons div
    card_pattern = re.compile(
        r'class="card-item".*?'
        r'<h2 class="card-title">([^<]+)</h2>'    # group 1: name
        r'.*?company-tag-block">(.*?)</div>'        # group 2: tags block
        r'.*?card-description1[^>]*>(.*?)</div>'   # group 3: description
        r'.*?company-link-item" href="([^"]+)"'    # group 4: website URL
        r'.*?social-media-icons">(.*?)</div>',     # group 5: social icons block
        re.DOTALL,
    )

    cards: list[dict] = []
    for m in card_pattern.finditer(html):
        name = m.group(1).strip()
        tags_html = m.group(2)
        desc_raw = m.group(3).strip()
        website_url = m.group(4).strip()
        social_html = m.group(5)

        # Parse individual tag items: year, location, sector tags
        tags = [
            t.strip()
            for t in re.findall(r'<p class="company-tag-item">\s*(.*?)\s*</p>', tags_html)
            if t.strip()
        ]

        # Derive founding year (4-digit number), location, and sector tags
        founding_year: str | None = None
        location: str | None = None
        sector_tags: list[str] = []
        for tag in tags:
            if re.fullmatch(r"\d{4}", tag):
                founding_year = tag
            elif re.search(r"[A-Za-z]{3,}", tag) and not re.search(
                r"(Tech|B2B|SaaS|AI|ML|IoT|EV|D2C|FinTech|EdTech|HealthTech|"
                r"AgriTech|DeepTech|CleanTech|ConsumerTech|Enterprise|Logistics|"
                r"Retail|Marketplace|Mobility|Gaming|Social|Media|Analytics|"
                r"Cybersecurity|Blockchain|AR/VR|HR|Legal)", tag
            ):
                # Likely a location (contains letters but not a known sector keyword)
                location = tag
            else:
                sector_tags.append(tag)

        # Extract LinkedIn URL
        linkedin_urls = re.findall(
            r'href="(https://(?:www\.)?linkedin\.com/[^"]+)"', social_html
        )
        linkedin_url = linkedin_urls[0] if linkedin_urls else None

        # Clean description (strip HTML tags, collapse whitespace)
        desc = re.sub(r"<[^>]+>", " ", desc_raw)
        desc = re.sub(r"\s+", " ", desc).strip() or None

        # Industry: first sector tag as a summary
        industry = sector_tags[0] if sector_tags else None

        cards.append(
            {
                "name": name,
                "founding_year": founding_year,
                "location": location,
                "sector_tags": sector_tags,
                "description": desc,
                "website_url": website_url,
                "linkedin_url": linkedin_url,
                "industry": industry,
            }
        )

    return cards


def main() -> int:
    print(f"Fetching Chiratae Ventures portfolio from {PORTFOLIO_URL} ...")
    html = _fetch_html()
    print(f"  Downloaded {len(html):,} bytes")

    all_cards = _extract_cards(html)
    print(f"  {len(all_cards)} card occurrences parsed (including category-tab duplicates)")

    counts = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_website": 0,
        "skipped_bad_domain": 0,
        "skipped_duplicate_in_run": 0,
    }
    seen_emails: set[str] = set()  # kept for pattern compliance; no contacts in this scraper
    seen_domains: set[str] = set()  # deduplicate across category tabs

    db = SessionLocal()
    try:
        for card in all_cards:
            website_url = card.get("website_url") or ""
            if not website_url:
                counts["skipped_no_website"] += 1
                continue

            domain = _parse_domain(website_url)
            if not domain:
                counts["skipped_bad_domain"] += 1
                continue

            # Deduplicate across category tabs within this run
            if domain in seen_domains:
                counts["skipped_duplicate_in_run"] += 1
                continue
            seen_domains.add(domain)

            company_name = card["name"] or domain
            description = card.get("description")
            industry = card.get("industry")

            # Upsert company by domain
            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    domain=domain,
                    name=company_name,
                    source=SOURCE_TAG,
                    funding_stage=None,
                    industry=industry,
                    description=description,
                )
                db.add(company)
                db.flush()  # get company.id before inserting contacts
                counts["companies_created"] += 1
            else:
                counts["companies_existing"] += 1

            # No founder names or emails are exposed on the page.
            # If a LinkedIn URL is available, create a stub contact with
            # no email so the company at least has a social anchor.
            linkedin_url = card.get("linkedin_url")
            if linkedin_url:
                # Check if we already have a stub contact for this company with this linkedin
                existing_contact = db.scalar(
                    select(Contact).where(
                        Contact.company_id == company.id,
                        Contact.linkedin_url == linkedin_url,
                    )
                )
                if existing_contact is None:
                    stub = Contact(
                        company_id=company.id,
                        name=None,
                        email=None,
                        role="Founder",
                        email_verified=False,
                        email_confidence=None,
                        scraped_pattern=None,
                        linkedin_url=linkedin_url,
                        source=SOURCE_TAG,
                    )
                    db.add(stub)
                    counts["contacts_created"] += 1
                else:
                    counts["contacts_existing"] += 1

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
