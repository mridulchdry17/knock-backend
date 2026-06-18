"""Scrape Peak XV Partners (formerly Sequoia India) portfolio via static HTML.

For each portfolio company:
  - crawls all listing pages to collect company slugs
  - fetches each /companies/[slug] detail page and parses HTML
  - extracts name, website, founders, description, sector, stage,
    founded year, partnership year, regions, LinkedIn URL
  - derives domain from website URL
  - constructs firstname@domain for each founder (guessed email)
  - upserts Company + Contact into the pool

Usage:
    .venv/bin/python -m scripts.scrape_peak_xv_partners_formerly_sequoia_india

Idempotent — re-runs skip existing emails. Records scraped_pattern as
"firstname" so the bounce-handler can try alternates later.
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

SOURCE_TAG = "peak-xv-partners-formerly-sequoia-india-scraping"
BASE_URL = "https://www.peakxv.com"

# Both pagination keys used on /our-companies
PAGINATION_KEYS = ["e6a89e8b", "577908b4"]
PAGES = [1, 2, 3, 4]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _fetch(url: str, retries: int = 3, delay: float = 1.0) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            if attempt == retries - 1:
                raise
            print(f"    [warn] fetch {url} attempt {attempt + 1} failed: {exc}; retrying…")
            time.sleep(delay * (attempt + 1))
    return ""  # unreachable


# ---------------------------------------------------------------------------
# Slug collection
# ---------------------------------------------------------------------------

def _collect_slugs() -> list[str]:
    """Return deduplicated list of company slugs from all listing pages."""
    seen: set[str] = set()
    slug_re = re.compile(r'href="/companies/([^"]+)"')

    for key in PAGINATION_KEYS:
        for page in PAGES:
            if page == 1:
                url = f"{BASE_URL}/our-companies"
            else:
                url = f"{BASE_URL}/our-companies?{key}_page={page}"
            print(f"  Fetching listing page: {url}")
            try:
                html = _fetch(url)
                for m in slug_re.finditer(html):
                    slug = m.group(1).strip()
                    if slug and slug not in seen:
                        seen.add(slug)
            except Exception as exc:
                print(f"    [warn] could not fetch listing page {url}: {exc}")
            time.sleep(0.3)

    return sorted(seen)


# ---------------------------------------------------------------------------
# Detail page parsing
# ---------------------------------------------------------------------------

def _extract_text_after_label(html: str, label: str) -> str | None:
    """
    Find a label like 'Founded', 'Partnered', 'current Stage', etc.
    inside text-style-tagline spans, then grab the next text value.
    Pattern in HTML:
      <div class="text-style-tagline is-style-s3">Founded</div><div>2014</div>
    """
    # Case-insensitive label search within tagline div
    pattern = re.compile(
        r'text-style-tagline[^>]*>[^<]*' + re.escape(label) + r'[^<]*</div>\s*<div[^>]*>([^<]+)',
        re.IGNORECASE | re.DOTALL,
    )
    m = pattern.search(html)
    if m:
        return m.group(1).strip()
    return None


def _extract_multi_value(html: str, label: str) -> list[str]:
    """
    Extract list items after a tagline label (e.g. sector, region, founders).
    HTML pattern:
      <div class="text-style-tagline ...">sector</div>
      <div class="display-block w-dyn-list">
        <div role="list" ...><div role="listitem" ...><div ...><div>FinTech</div>...
    """
    # Find the label first
    label_re = re.compile(
        r'text-style-tagline[^>]*>[^<]*' + re.escape(label) + r'[^<]*</div>'
        r'(.*?)</div>\s*</div>\s*</div>',
        re.IGNORECASE | re.DOTALL,
    )
    m = label_re.search(html)
    if not m:
        return []
    block = m.group(1)
    # Pull all text inside innermost divs
    items = re.findall(r'<div[^>]*>\s*([A-Za-z][^<]{0,120}?)\s*</div>', block)
    results = []
    for item in items:
        item = item.strip()
        # Skip CSS classes, empty strings, separators
        if item and len(item) < 100 and not item.startswith(".") and ";" not in item:
            results.append(item)
    return results


def _parse_detail(html: str, slug: str) -> dict:
    """Parse a company detail page and return a data dict."""
    result: dict = {
        "name": None,
        "website": None,
        "linkedin_url": None,
        "description": None,
        "sector": None,
        "stage": None,
        "founded_year": None,
        "partnership_year": None,
        "regions": [],
        "founders": [],
        "real_emails": [],
    }

    # --- Company name: from <h1 ...>Name</h1> or og:title ---
    h1_m = re.search(r'<h1[^>]*>\s*([^<]+)\s*</h1>', html)
    if h1_m:
        result["name"] = h1_m.group(1).strip()
    if not result["name"]:
        og_title_m = re.search(r'og:title[^>]*content="([^"|]+)\s*\|', html)
        if og_title_m:
            result["name"] = og_title_m.group(1).strip()
    if not result["name"]:
        result["name"] = slug.replace("-", " ").title()

    # --- Website ---
    # Raw HTML pattern:
    #   >Website</div><a ... href="https://www.razorpay.com" target="_blank" class="client-info_link ...">
    #     <div data-link-text="">https://www.razorpay.com</div>
    # Match the anchor immediately after the ">Website</div>" text.
    website_m = re.search(
        r'>Website</div>\s*<a[^>]+href="(https?://[^"]+)"',
        html,
        re.IGNORECASE,
    )
    if website_m:
        url_val = website_m.group(1).strip()
        if "peakxv.com" not in url_val and "linkedin.com" not in url_val:
            result["website"] = url_val

    # Fallback: data-link-text div containing a URL (not peakxv/linkedin)
    if not result["website"]:
        dltext_m = re.search(r'data-link-text[^>]*>\s*(https?://[^<\s]+)', html)
        if dltext_m:
            url_val = dltext_m.group(1).strip()
            if "peakxv.com" not in url_val and "linkedin.com" not in url_val:
                result["website"] = url_val

    # --- LinkedIn URL ---
    linkedin_m = re.search(r'href="(https?://(?:www\.)?linkedin\.com/company/[^"]+)"', html)
    if linkedin_m:
        result["linkedin_url"] = linkedin_m.group(1).strip()

    # --- Description: <p class="text-size-medium">...</p> ---
    desc_m = re.search(r'<p[^>]*class="text-size-medium"[^>]*>\s*([^<]+)\s*</p>', html)
    if desc_m:
        result["description"] = desc_m.group(1).strip()
    if not result["description"]:
        og_desc_m = re.search(r'name="description"\s+content="([^"]+)"', html)
        if og_desc_m:
            result["description"] = og_desc_m.group(1).strip()

    # --- Founded year ---
    founded = _extract_text_after_label(html, "Founded")
    if founded and re.match(r"^\d{4}$", founded.strip()):
        result["founded_year"] = founded.strip()

    # --- Partnership year ---
    partnered = _extract_text_after_label(html, "Partnered")
    if partnered and re.match(r"^\d{4}$", partnered.strip()):
        result["partnership_year"] = partnered.strip()

    # --- Stage ---
    stage = _extract_text_after_label(html, "current Stage")
    if stage:
        result["stage"] = stage.strip()

    # --- Sector ---
    # HTML: <div class="text-style-tagline ...">sector</div>
    #       <div class="display-block w-dyn-list"><div role="list" ...>
    #         <div role="listitem" ...><div class="client-info_link"><div>FinTech</div></div>
    sector_m = re.search(
        r'>sector<.*?client-info_link[^>]*><div>([^<]+)</div>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if sector_m:
        result["sector"] = sector_m.group(1).strip()

    # --- Regions ---
    region_m = re.search(
        r'>region<.*?client-info_meta-list[^>]*>(.*?)</div>\s*</div>\s*</div>\s*</div>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if region_m:
        block = region_m.group(1)
        # Each region is: <div role="listitem" ...><div>REGION</div></div>
        regions = re.findall(r'<div[^>]*>\s*<div>\s*([A-Za-z][^<]{0,30}?)\s*</div>', block)
        result["regions"] = [r.strip() for r in regions if r.strip()]

    # --- Founders ---
    # The Founders section uses <div class="client-info_link has-underline"><div>Name</div></div>
    # This class is ONLY used for founder names on the detail pages.
    founder_names = re.findall(
        r'client-info_link has-underline"><div>([^<]+)</div>',
        html,
    )
    result["founders"] = [f.strip() for f in founder_names if f.strip()]

    # --- Real emails ---
    emails_found = re.findall(r'[\w.+\-]+@[\w\-]+\.[a-z]{2,}', html)
    real_emails = [
        e for e in emails_found
        if "peakxv.com" not in e
        and "webflow.com" not in e
        and "@example" not in e
        and "jsdelivr" not in e
    ]
    result["real_emails"] = real_emails

    return result


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

def _parse_domain(website: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(website)
        host = (parsed.netloc or parsed.path).lower()
        host = host.removeprefix("www.")
        # Remove port
        host = host.split(":")[0]
        return host if "." in host else None
    except Exception:
        return None


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=== Peak XV Partners portfolio scraper ===")
    print("Step 1: Collecting company slugs from listing pages…")
    slugs = _collect_slugs()
    print(f"  Found {len(slugs)} unique company slugs")

    counts = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_website": 0,
        "skipped_bad_domain": 0,
        "skipped_fetch_error": 0,
    }
    seen_emails: set[str] = set()

    print("\nStep 2: Fetching company detail pages…")
    db = SessionLocal()
    try:
        for i, slug in enumerate(slugs, 1):
            url = f"{BASE_URL}/companies/{slug}"
            print(f"  [{i}/{len(slugs)}] {slug}")

            try:
                html = _fetch(url)
            except Exception as exc:
                print(f"    [error] fetch failed: {exc}")
                counts["skipped_fetch_error"] += 1
                continue

            data = _parse_detail(html, slug)

            website = data["website"]
            if not website:
                print(f"    [skip] no website found")
                counts["skipped_no_website"] += 1
                continue

            domain = _parse_domain(website)
            if not domain:
                print(f"    [skip] bad domain from: {website}")
                counts["skipped_bad_domain"] += 1
                continue

            company_name = data["name"] or domain
            stage = data["stage"]
            sector = data["sector"]
            description = data["description"]

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
                db.flush()
                counts["companies_created"] += 1
                print(f"    + company created: {company_name} ({domain})")
            else:
                counts["companies_existing"] += 1
                print(f"    ~ company exists: {company_name} ({domain})")

            # Contacts: try real emails first, then guess from founders
            founders = data["founders"]
            real_emails = data.get("real_emails", [])
            linkedin_url = data.get("linkedin_url")

            # Map real emails to founders if counts match
            founder_email_map: dict[str, str | None] = {}
            if founders:
                for fn in founders:
                    founder_email_map[fn] = None

            # If real emails are found and count matches founders, pair them
            if real_emails and founders and len(real_emails) == len(founders):
                for fn, em in zip(founders, real_emails):
                    founder_email_map[fn] = em

            if founders:
                for full_name in founders:
                    real_email = founder_email_map.get(full_name)

                    if real_email:
                        email = real_email.lower()
                        email_verified = True
                        email_confidence = 95
                        scraped_pattern = None
                    else:
                        first = _first_name(full_name)
                        if not first:
                            continue
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
                        seen_emails.add(email)
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
                    print(f"    + contact: {full_name} <{email}>")
            elif not founders and linkedin_url:
                # No founders found but we have a LinkedIn URL — create a placeholder contact
                # Only if we can at least get a company-level LinkedIn
                pass

            # Polite crawl rate
            time.sleep(0.4)

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
