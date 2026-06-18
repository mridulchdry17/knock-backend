"""Scrape 8i Ventures portfolio via static HTML pages.

For each portfolio company:
  - parses sitemap for all company slugs
  - fetches each /companies/[slug] page
  - extracts company name, website URL, founder names, LinkedIn URLs
  - derives domain from website URL
  - if real email found in HTML: email_verified=True, confidence=95
  - otherwise guesses firstname@domain: email_verified=False, confidence=60
  - upserts Company + Contact into the pool

Usage:
    .venv/bin/python -m scripts.scrape_8i_ventures

Idempotent — re-runs skip existing emails. Records the scraped_pattern as
"firstname" so the bounce-handler can try alternates (first.last, etc.) later.
"""
from __future__ import annotations

import re
import sys
import time
import urllib.parse
import urllib.request
from xml.etree import ElementTree

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company, Contact

SOURCE_TAG = "8i-ventures-scraping"
BASE_URL = "https://8ivc.com"
SITEMAP_URL = "https://8ivc.com/sitemap.xml"

# Social/utility domains to skip when hunting for the company website
SKIP_DOMAINS = {
    "linkedin.com", "twitter.com", "x.com", "instagram.com",
    "facebook.com", "youtube.com", "t.co", "8ivc.com",
    "framer.com", "framerusercontent.com",
}

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
LINKEDIN_IN_RE = re.compile(r'https?://(?:www\.)?linkedin\.com/in/[^"\'>\s]+')
LINKEDIN_CO_RE = re.compile(r'https?://(?:www\.)?linkedin\.com/company/[^"\'>\s]+')


def _fetch_url(url: str, retries: int = 3) -> str:
    """Fetch a URL and return decoded text, with simple retry logic."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                charset = "utf-8"
                ct = resp.headers.get("Content-Type", "")
                m = re.search(r"charset=([\w-]+)", ct)
                if m:
                    charset = m.group(1)
                return raw.decode(charset, errors="replace")
        except Exception as exc:
            if attempt == retries - 1:
                raise
            print(f"    Retry {attempt + 1} for {url}: {exc}")
            time.sleep(2)
    return ""


def _fetch_sitemap_slugs() -> list[str]:
    """Return all /companies/<slug> paths from the sitemap."""
    print(f"Fetching sitemap: {SITEMAP_URL}")
    xml_text = _fetch_url(SITEMAP_URL)
    # Strip namespace for simpler parsing
    xml_text = re.sub(r'\s+xmlns(?::\w+)?="[^"]+"', "", xml_text)
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        # Fallback: regex
        locs = re.findall(r"<loc>(https://8ivc\.com/companies/[^<]+)</loc>", xml_text)
        return [loc for loc in locs if loc.rstrip("/") != "https://8ivc.com/companies"]

    slugs: list[str] = []
    for url_el in root.iter("url"):
        loc_el = url_el.find("loc")
        if loc_el is None or loc_el.text is None:
            continue
        loc = loc_el.text.strip().rstrip("/")
        # Match /companies/<slug> but not the index page itself
        if re.match(r"https://8ivc\.com/companies/[^/]+$", loc):
            slugs.append(loc)
    return slugs


def _parse_domain(website: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(website)
        host = parsed.netloc or parsed.path
        host = host.lower().removeprefix("www.")
        return host if "." in host else None
    except Exception:
        return None


def _extract_company_name(html: str) -> str:
    # Try <title> first
    m = re.search(r"<title[^>]*>\s*([^<|–\-]+)", html, re.IGNORECASE)
    if m:
        name = m.group(1).strip()
        if name and name.lower() not in ("8i ventures", ""):
            return name
    # Try og:title
    m = re.search(r'property="og:title"[^>]*content="([^"]+)"', html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def _extract_website(html: str) -> str | None:
    """Find the first external link that isn't a social/utility domain."""
    # Look for all <a href="..."> that start with http
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
    for href in hrefs:
        if not href.startswith("http"):
            continue
        parsed = urllib.parse.urlparse(href)
        domain = parsed.netloc.lower().removeprefix("www.")
        skip = False
        for skip_d in SKIP_DOMAINS:
            if domain == skip_d or domain.endswith("." + skip_d):
                skip = True
                break
        if skip:
            continue
        # Must have a real TLD
        if "." in domain:
            return href
    return None


def _extract_founders(html: str) -> list[str]:
    """Extract founder names from <h5> tags."""
    # h5 tags often hold founder names on Framer pages
    h5_names = re.findall(r"<h5[^>]*>\s*(.*?)\s*</h5>", html, re.IGNORECASE | re.DOTALL)
    founders: list[str] = []
    for raw in h5_names:
        # Strip tags
        name = re.sub(r"<[^>]+>", "", raw).strip()
        # Skip empty, too long, or clearly not names
        if not name or len(name) > 80 or "\n" in name:
            continue
        # Must look like a name: at least two words of alpha chars
        if re.match(r"^[A-Za-z][a-zA-Z'\-]+(?: [A-Za-z][a-zA-Z'\-]+)+$", name):
            founders.append(name)
    return founders


def _extract_linkedin_personal(html: str) -> list[str]:
    return list(dict.fromkeys(LINKEDIN_IN_RE.findall(html)))


def _extract_linkedin_company(html: str) -> str | None:
    m = LINKEDIN_CO_RE.search(html)
    return m.group(0) if m else None


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


def _scrape_company_page(page_url: str) -> dict:
    """Fetch one company page and return extracted data."""
    html = _fetch_url(page_url)
    name = _extract_company_name(html)
    website = _extract_website(html)
    founders = _extract_founders(html)
    linkedin_personal = _extract_linkedin_personal(html)
    linkedin_company = _extract_linkedin_company(html)
    real_emails = EMAIL_RE.findall(html)
    # Filter out generic/noreply emails
    real_emails = [
        e for e in real_emails
        if not any(skip in e.lower() for skip in ("noreply", "no-reply", "example", "sentry", "8ivc"))
    ]
    return {
        "name": name,
        "website": website,
        "founders": founders,
        "linkedin_personal": linkedin_personal,
        "linkedin_company": linkedin_company,
        "real_emails": real_emails,
    }


def main() -> int:
    print("Fetching 8i Ventures portfolio from static HTML pages...")
    slugs = _fetch_sitemap_slugs()
    print(f"  Found {len(slugs)} company page(s) in sitemap")

    counts = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_website": 0,
        "skipped_bad_domain": 0,
        "skipped_no_name": 0,
    }
    seen_emails: set[str] = set()

    db = SessionLocal()
    try:
        for page_url in slugs:
            slug = page_url.rstrip("/").rsplit("/", 1)[-1]
            print(f"\nProcessing: {slug}")
            try:
                data = _scrape_company_page(page_url)
            except Exception as exc:
                print(f"  ERROR fetching {page_url}: {exc}")
                counts["skipped_no_website"] += 1
                continue

            company_name = data["name"] or slug.replace("-", " ").title()
            website = data["website"]
            founders = data["founders"]
            linkedin_personal_urls = data["linkedin_personal"]
            real_emails = data["real_emails"]

            print(f"  name={company_name!r}  website={website!r}  founders={founders}")

            if not website:
                print("  -> skipping: no external website found")
                counts["skipped_no_website"] += 1
                continue

            domain = _parse_domain(website)
            if not domain:
                print(f"  -> skipping: bad domain from {website!r}")
                counts["skipped_bad_domain"] += 1
                continue

            # Upsert company by domain
            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    domain=domain,
                    name=company_name,
                    source=SOURCE_TAG,
                )
                db.add(company)
                db.flush()
                counts["companies_created"] += 1
                print(f"  -> Company CREATED: {company_name} ({domain})")
            else:
                counts["companies_existing"] += 1
                print(f"  -> Company EXISTS: {company_name} ({domain})")

            # Build a map: founder_name -> linkedin_url (by index if lists align)
            # linkedin_personal_urls might have more entries than founders; pair by order
            founder_linkedin_map: dict[str, str | None] = {}
            for i, founder in enumerate(founders):
                li_url = linkedin_personal_urls[i] if i < len(linkedin_personal_urls) else None
                founder_linkedin_map[founder] = li_url

            # Process founders
            if founders:
                for full_name in founders:
                    first = _first_name(full_name)
                    linkedin_url = founder_linkedin_map.get(full_name)

                    # Try to find a real email for this founder
                    real_email: str | None = None
                    for em in real_emails:
                        if first in em.lower():
                            real_email = em
                            break

                    if real_email:
                        email = real_email
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
                    print(f"  -> Contact CREATED: {full_name} <{email}>")
            elif linkedin_personal_urls:
                # No named founders extracted but we have LinkedIn URLs — store without email
                for li_url in linkedin_personal_urls:
                    contact = Contact(
                        company_id=company.id,
                        name=None,
                        email=None,
                        role="Founder",
                        email_verified=False,
                        email_confidence=None,
                        scraped_pattern=None,
                        linkedin_url=li_url,
                        source=SOURCE_TAG,
                    )
                    db.add(contact)
                    counts["contacts_created"] += 1
                    print(f"  -> Contact CREATED (LinkedIn only): {li_url}")
            else:
                print("  -> No founders found; company-only record")

            # Be polite
            time.sleep(0.5)

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
