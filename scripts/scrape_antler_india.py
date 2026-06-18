"""Scrape Antler India portfolio via the public Webflow-hosted static portfolio page.

Strategy:
  1. Fetch https://www.antler.co/portfolio and subsequent pagination pages
     (?0b933bfd_page=N) until no next-page link is found.
  2. For each portco card, parse:
       - Company name  (fs-cmsfilter-field="name")
       - Description   (fs-cmsfilter-field="description")
       - Location      (first tag_small_text — always the location)
       - Sector        (second tag_small_text)
       - Year          (third tag_small_text)
       - Website URL   (clickable_link <a href="...">)
  3. Keep only cards where location == "India".
  4. Parse domain from website URL.
  5. No founder data is present in the cards — create Company rows only,
     no Contact rows.
  6. Upsert Company into the pool (idempotent on re-run).

Note: Founders are NOT included in the Antler portfolio cards — only
company-level data is available from static HTML. Contact rows are
therefore not created by this scraper.

Usage:
    .venv/bin/python -m scripts.scrape_antler_india
"""
from __future__ import annotations

import re
import sys
import time
import urllib.parse
import urllib.request

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company

SOURCE_TAG = "antler-india-scraping"
BASE_URL = "https://www.antler.co/portfolio"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _fetch(url: str, timeout: int = 30, retries: int = 3) -> str:
    """Fetch URL with retry on transient errors."""
    req = urllib.request.Request(url, headers=HEADERS)
    last_err: Exception = RuntimeError("no attempts made")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                wait = 5 * (2 ** attempt)
                print(f"  [429 rate-limit, waiting {wait}s]", flush=True)
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise last_err


def _parse_domain(website: str) -> str | None:
    """Extract bare domain from a URL."""
    try:
        parsed = urllib.parse.urlparse(website)
        host = parsed.netloc or parsed.path
        host = host.lower().removeprefix("www.")
        host = host.split(":")[0].rstrip("/")
        return host if "." in host else None
    except Exception:
        return None


def _parse_cards(html: str) -> list[dict]:
    """Extract all portco card data from a page of HTML."""
    companies: list[dict] = []

    # Each card starts with <div class="portco_card">
    # We parse between consecutive card starts.
    card_starts = [m.start() for m in re.finditer(r'<div class="portco_card">', html)]

    for i, start in enumerate(card_starts):
        end = card_starts[i + 1] if i + 1 < len(card_starts) else len(html)
        card = html[start:end]

        # Company name
        name_m = re.search(r'fs-cmsfilter-field="name"[^>]*>([^<]+)', card)
        company_name = name_m.group(1).strip() if name_m else None

        # Description
        desc_m = re.search(r'fs-cmsfilter-field="description"[^>]*>([^<]+)', card)
        description = desc_m.group(1).strip() if desc_m else None

        # Tags: location, sector, year — always in that order
        tags = re.findall(r'class="tag_small_text">([^<]+)', card)
        location = tags[0].strip() if len(tags) > 0 else None
        sector = tags[1].strip() if len(tags) > 1 else None
        year_raw = tags[2].strip() if len(tags) > 2 else None

        # Only keep India companies
        if location != "India":
            continue

        # Website URL from clickable_link anchor
        href_m = re.search(
            r'class="clickable_link[^"]*"[^>]+href="(https?://[^"]+)"', card
        )
        if not href_m:
            # Fallback: any external href in the card
            href_m = re.search(r'href="(https?://(?!cdn\.)[^"]+)"', card)
        website_url = href_m.group(1).strip() if href_m else None

        companies.append(
            {
                "name": company_name,
                "description": description,
                "location": location,
                "sector": sector,
                "year": year_raw,
                "website_url": website_url,
            }
        )

    return companies


def _fetch_all_india_companies() -> list[dict]:
    """Paginate through all portfolio pages, collecting India companies."""
    all_companies: list[dict] = []
    page = 1

    while True:
        if page == 1:
            url = BASE_URL
        else:
            url = f"{BASE_URL}?0b933bfd_page={page}"

        print(f"  Fetching page {page}: {url}", flush=True)
        html = _fetch(url)
        page_companies = _parse_cards(html)
        all_companies.extend(page_companies)

        # Check if there is a next page link
        next_page = page + 1
        if f"?0b933bfd_page={next_page}" in html:
            page = next_page
            time.sleep(1.0)  # polite rate limit
        else:
            break

    return all_companies


def main() -> int:
    print("Fetching Antler India portfolio from static HTML pages...")
    raw_companies = _fetch_all_india_companies()

    # Deduplicate by name (in case a company appears on multiple pages)
    seen_names: set[str] = set()
    companies: list[dict] = []
    for c in raw_companies:
        name_key = (c.get("name") or "").lower().strip()
        if name_key and name_key not in seen_names:
            seen_names.add(name_key)
            companies.append(c)

    print(f"  {len(companies)} unique India companies found")

    counts = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_website": 0,
        "skipped_bad_domain": 0,
    }

    for record in companies:
        name = record.get("name") or ""
        website = record.get("website_url") or ""
        description = record.get("description") or None
        sector = record.get("sector") or None
        year = record.get("year") or None

        if not website:
            print(f"    SKIP (no website): {name}")
            counts["skipped_no_website"] += 1
            continue

        domain = _parse_domain(website)
        if not domain:
            print(f"    SKIP (bad domain): {name} -> {website}")
            counts["skipped_bad_domain"] += 1
            continue

        company_name = name or domain

        db = SessionLocal()
        try:
            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    domain=domain,
                    name=company_name,
                    source=SOURCE_TAG,
                    industry=sector,
                    description=description,
                )
                db.add(company)
                db.flush()
                counts["companies_created"] += 1
                print(f"    NEW: {company_name} ({domain})")
            else:
                counts["companies_existing"] += 1
                print(f"    EXISTS: {company_name} ({domain})")

            # No founder/contact data available from Antler portfolio cards.
            # Contacts are intentionally skipped.

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
