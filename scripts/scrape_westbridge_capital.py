"""Scrape WestBridge Capital's portfolio via their public static HTML pages.

Strategy:
  1. Parse sitemap.xml to enumerate all 170 /portfolio/[slug] URLs.
  2. Fetch each company page (fully server-rendered HTML, no JS needed).
  3. Extract: company name, website URL, founders, founded year, sector,
     description, investment stage, and company LinkedIn URL.
  4. For each founder: if a real email is found in the HTML use it
     (email_verified=True, confidence=95); otherwise guess firstname@domain
     (email_verified=False, confidence=60, scraped_pattern="firstname").

Usage:
    .venv/bin/python -m scripts.scrape_westbridge_capital

Idempotent — re-runs skip existing emails / domains.
"""
from __future__ import annotations

import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company, Contact

SOURCE_TAG = "westbridge-capital-scraping"
SITEMAP_URL = "https://westbridgecap.com/sitemap.xml"
BASE_URL = "https://westbridgecap.com"

# Courtesy delay between requests (seconds)
REQUEST_DELAY = 0.4


def _fetch_url(url: str, timeout: int = 30) -> str:
    """Fetch a URL and return decoded HTML text."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; WestBridgeScraper/1.0; "
                "+https://github.com/knock)"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _fetch_portfolio_slugs() -> list[str]:
    """Parse sitemap.xml and return all /portfolio/<slug> URLs."""
    xml_text = _fetch_url(SITEMAP_URL)
    root = ET.fromstring(xml_text)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    slugs = []
    for url_el in root.findall("sm:url", ns):
        loc = (url_el.findtext("sm:loc", namespaces=ns) or "").strip()
        # Match /portfolio/<slug> but NOT just /portfolio
        if re.match(r"https://westbridgecap\.com/portfolio/[^/]+$", loc):
            slugs.append(loc)
    return slugs


def _parse_domain(website: str) -> str | None:
    """Extract bare domain (no www. prefix) from a URL."""
    try:
        parsed = urllib.parse.urlparse(website)
        host = (parsed.netloc or parsed.path).lower().strip()
        host = host.removeprefix("www.")
        return host if "." in host else None
    except Exception:
        return None


def _clean_text(raw: str) -> str:
    """Collapse whitespace in an HTML-extracted string."""
    return re.sub(r"\s+", " ", raw).strip()


def _parse_field(html: str, label: str) -> str | None:
    """
    Extract the content of a definition block for the given label.
    Pattern on the page:
        <strong ...>Label:</strong>
        <div ... role="definition" ...>VALUE</div>
    """
    pattern = (
        r"<strong[^>]*>\s*" + re.escape(label) + r"\s*:\s*</strong>"
        r"\s*<div[^>]*role=\"definition\"[^>]*>(.*?)</div>"
    )
    m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    return _clean_text(re.sub(r"<[^>]+>", " ", m.group(1)))


def _parse_all_definition_labels(html: str) -> dict[str, str]:
    """Return all label→value pairs from the definition list on the page."""
    pattern = (
        r"<strong[^>]*>\s*([^<]+?)\s*:\s*</strong>"
        r"\s*<div[^>]*role=\"definition\"[^>]*>(.*?)</div>"
    )
    result: dict[str, str] = {}
    for label, content in re.findall(pattern, html, re.DOTALL):
        result[label.strip()] = _clean_text(re.sub(r"<[^>]+>", " ", content))
    return result


def _parse_founders_html(html: str, label: str = "Founders") -> list[str]:
    """
    Extract founder names from the definition block.
    The names may be plain text <li> items or wrapped in <a> links.
    """
    pattern = (
        r"<strong[^>]*>\s*" + re.escape(label) + r"\s*:\s*</strong>"
        r"\s*<div[^>]*role=\"definition\"[^>]*>(.*?)</div>"
    )
    m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
    if not m:
        return []
    block = m.group(1)
    # Strip all tags, split on newlines to get individual names
    text = re.sub(r"<[^>]+>", "\n", block)
    names = [n.strip() for n in text.splitlines() if n.strip()]
    # Filter noise
    filtered = []
    for n in names:
        if re.fullmatch(r"[-–—N/Atbd.]+", n, re.IGNORECASE):
            continue
        if len(n) < 2 or len(n) > 80:
            continue
        filtered.append(n)
    return filtered


def _parse_company_name(html: str, slug: str) -> str:
    """Extract company name from <title> tag, falling back to slug."""
    m = re.search(r"<title>([^<]+)</title>", html)
    if m:
        # Title format: "CompanyName - WestBridge Capital • Westbridge Capital"
        raw = m.group(1).split(" - ")[0].strip()
        if raw:
            return raw
    # Fallback: capitalise slug
    return slug.replace("-", " ").title()


def _parse_website(html: str) -> str | None:
    """
    Extract the company's external website URL.
    It appears as an <a> with class 'inline-flex items-center rounded-full ...'
    and target='_blank', pointing to a non-WestBridge URL.
    """
    # Primary pattern: the CTA button with the company's direct URL
    pattern = (
        r'<a\s+target="_blank"\s+rel="noopener noreferrer"\s+'
        r'class="inline-flex items-center rounded-full[^"]*"\s+'
        r'href="([^"]+)"'
    )
    for m in re.finditer(pattern, html):
        url = m.group(1)
        if (
            "westbridgecap" not in url
            and "linkedin" not in url
            and "twitter" not in url
            and "citco" not in url
            and url.startswith("http")
        ):
            return url
    return None


def _parse_company_linkedin(html: str) -> str | None:
    """
    Extract the portfolio company's LinkedIn URL.
    The page contains WestBridge's own LinkedIn (westbridgecapital) plus
    the company's LinkedIn — we want the non-WestBridge one.
    """
    links = re.findall(r'href="(https://www\.linkedin\.com/company/[^"]+)"', html)
    for link in links:
        if "westbridgecapital" not in link:
            return link.rstrip("/")
    return None


def _find_real_emails(text: str) -> list[str]:
    """Scan raw text for real email addresses."""
    return re.findall(r"[\w.+\-]+@[\w\-]+\.[a-z]{2,}", text, re.IGNORECASE)


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


def _parse_stage_from_defs(defs: dict[str, str]) -> str | None:
    """
    WestBridge labels the stage field differently per company, e.g.
    'Series B Investment', 'Public Investment', 'Seed Investment'.
    Find any key that contains 'Investment' or 'Stage'.
    """
    for key in defs:
        if "investment" in key.lower() or "stage" in key.lower():
            return key  # Use the label itself as the stage description
    return None


def _scrape_page(url: str) -> dict | None:
    """Fetch a single portfolio page and return extracted data dict."""
    slug = url.rstrip("/").split("/")[-1]
    try:
        html = _fetch_url(url)
    except Exception as exc:
        print(f"  [WARN] Failed to fetch {url}: {exc}")
        return None

    name = _parse_company_name(html, slug)
    website = _parse_website(html)
    defs = _parse_all_definition_labels(html)
    founders = _parse_founders_html(html, "Founders")
    sector = defs.get("Sector") or None
    founded = defs.get("Founded") or None
    stage = _parse_stage_from_defs(defs)
    company_linkedin = _parse_company_linkedin(html)

    # Description: the first meaningful paragraph in the body text area
    desc_m = re.search(
        r'<div[^>]*class="f-body-2[^"]*"[^>]*>\s*<p>([^<]{40,})</p>',
        html,
        re.DOTALL,
    )
    description: str | None = None
    if desc_m:
        description = _clean_text(desc_m.group(1))[:1024] or None

    # Scan page for any real email addresses
    real_emails = _find_real_emails(html)

    return {
        "name": name,
        "website": website,
        "founders": founders,
        "sector": sector,
        "founded": founded,
        "stage": stage,
        "description": description,
        "company_linkedin": company_linkedin,
        "real_emails": real_emails,
        "slug": slug,
    }


def _upsert_company_and_contacts(
    data: dict,
    counts: dict,
    seen_emails: set[str],
) -> None:
    """
    Open a fresh DB session, upsert one company + its contacts, commit, close.
    Using a fresh session per company avoids Turso's remote stream timeout
    which kills long-lived connections after ~2-3 minutes.
    """
    website = data["website"]
    domain = _parse_domain(website)
    if not domain:
        counts["skipped_bad_domain"] += 1
        return

    db = SessionLocal()
    try:
        # --- Upsert Company ---
        company = db.scalar(select(Company).where(Company.domain == domain))
        if company is None:
            company = Company(
                domain=domain,
                name=data["name"],
                source=SOURCE_TAG,
                funding_stage=data["stage"],
                industry=data["sector"],
                description=data["description"],
            )
            db.add(company)
            db.flush()  # get company.id before inserting contacts
            counts["companies_created"] += 1
            data["_status"] = "NEW"
        else:
            counts["companies_existing"] += 1
            data["_status"] = "EXISTS"

        founders = data["founders"]
        real_emails = data["real_emails"]
        company_linkedin = data["company_linkedin"]

        if not founders:
            db.commit()
            data["_contacts_added"] = 0
            return

        # --- Upsert Contacts ---
        contacts_added = 0
        for full_name in founders:
            first = _first_name(full_name)
            if not first:
                continue

            # Priority 1: real email found directly in page HTML
            real_email_match: str | None = None
            for em in real_emails:
                if domain in em:
                    real_email_match = em.lower()
                    break

            if real_email_match:
                email = real_email_match
                email_verified = True
                email_confidence = 95
                scraped_pattern = None
            else:
                # Priority 2: guess firstname@domain
                email = f"{first}@{domain}"
                email_verified = False
                email_confidence = 60
                scraped_pattern = "firstname"

            if email in seen_emails:
                counts["contacts_existing"] += 1
                continue
            existing = db.scalar(
                select(Contact).where(Contact.email == email)
            )
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
                linkedin_url=company_linkedin,  # company-level LinkedIn
                source=SOURCE_TAG,
            )
            db.add(contact)
            counts["contacts_created"] += 1
            contacts_added += 1

        db.commit()
        data["_contacts_added"] = contacts_added
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> int:
    print("Fetching WestBridge Capital portfolio from sitemap...")
    portfolio_urls = _fetch_portfolio_slugs()
    print(f"  {len(portfolio_urls)} portfolio pages found in sitemap")

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

    for idx, page_url in enumerate(portfolio_urls, 1):
        slug = page_url.rstrip("/").split("/")[-1]
        print(f"  [{idx}/{len(portfolio_urls)}] {slug} ...", end=" ", flush=True)

        data = _scrape_page(page_url)
        if data is None:
            counts["skipped_fetch_error"] += 1
            print("FETCH_ERROR")
            time.sleep(REQUEST_DELAY)
            continue

        website = data["website"]
        if not website:
            counts["skipped_no_website"] += 1
            print("NO_WEBSITE")
            time.sleep(REQUEST_DELAY)
            continue

        try:
            _upsert_company_and_contacts(data, counts, seen_emails)
        except Exception as exc:
            print(f"DB_ERROR: {exc}")
            time.sleep(REQUEST_DELAY)
            continue

        status = data.get("_status", "?")
        founders = data["founders"]
        contacts_added = data.get("_contacts_added", 0)

        if not founders:
            print(f"{status} | no founders")
        else:
            print(
                f"{status} | founders={len(founders)} "
                f"contacts_added={contacts_added}"
            )
        time.sleep(REQUEST_DELAY)

    print("\n=== Scrape complete ===")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
