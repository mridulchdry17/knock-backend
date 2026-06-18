"""Scrape Lightspeed India's portfolio via the public lsvp.com / lsip.com pages.

Two-step scrape:
  1. Parse https://lsvp.com/companies-india/ for all 96 India-focused companies
     (data-investor='both' or 'lsip'), extracting company name + detail URL.
  2. For each detail URL (on lsvp.com or lsip.com), fetch the page and extract:
       - company website (anchor id='company_url')
       - leadership names and titles (under the 'Leadership' heading)
       - stage invested, investment year, description

For each leadership person:
  - Role is set to "Founder" (per instructions).
  - Email priority:
      1. Real email found in HTML (email_verified=True, confidence=95)
      2. Guessed as firstname@domain (email_verified=False, confidence=60)
  - LinkedIn URLs extracted if present.

Usage:
    .venv/bin/python -m scripts.scrape_lightspeed_india

Idempotent — re-runs skip existing domains / emails.
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

SOURCE_TAG = "lightspeed-india-scraping"
PORTFOLIO_URL = "https://lsvp.com/companies-india/"

# Regex patterns
_EMAIL_RE = re.compile(r'[\w.+\-]+@[\w\-]+\.[a-z]{2,}', re.IGNORECASE)
_LINKEDIN_RE = re.compile(r'href="(https?://(?:www\.)?linkedin\.com/in/[^"]+)"')


def _ua_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
    )


def _fetch(url: str) -> str:
    req = _ua_request(url)
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


def _parse_name(full: str) -> str:
    """Extract the name portion before the first dash/hyphen (title separator)."""
    # Format: "Name - Title" or "Name – Title"
    parts = re.split(r"\s*[-–—]\s*", full, maxsplit=1)
    return parts[0].strip()


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


def _fetch_portfolio_companies() -> list[tuple[str, str, str]]:
    """Return list of (company_name, detail_url, investor_type)."""
    html = _fetch(PORTFOLIO_URL)
    companies = re.findall(
        r'<li[^>]+data-company-id="[^"]+"[^>]+data-investor="(both|lsip)".*?'
        r'<a href="([^"]+)"[^>]*>.*?<h5>\s*([^<]+?)\s*<',
        html,
        re.DOTALL,
    )
    result = []
    for investor_type, detail_url, raw_name in companies:
        name = re.sub(r"\s+", " ", raw_name).strip()
        result.append((name, detail_url.strip(), investor_type))
    return result


def _scrape_detail(detail_url: str) -> dict:
    """Return dict with keys: website, leadership, stage, investment_year, description."""
    try:
        html = _fetch(detail_url)
    except Exception as exc:
        print(f"    WARN: could not fetch {detail_url}: {exc}")
        return {}

    # --- Website URL ---
    # <a href="..." id="company_url"> OR href="..." id="company_url"
    website = None
    m = re.search(r'<a[^>]+id="company[_-]url"[^>]*href="([^"]+)"', html)
    if not m:
        m = re.search(r'href="([^"]+)"[^>]*id="company[_-]url"', html)
    if m:
        website = m.group(1).strip()

    # --- Leadership ---
    # Pattern: Leadership heading, followed by <div>Name - Title</div> entries
    leadership: list[str] = []
    idx = html.find("Leadership")
    if idx >= 0:
        block = html[idx : idx + 2000]
        # Grab all divs directly after the heading until we hit a different heading
        entries = re.findall(
            r"<div[^>]*>\s*([A-Z][a-z][^<]{3,80})\s*</div>",
            block,
        )
        for entry in entries:
            text = re.sub(r"<[^>]+>", "", entry).strip()
            # Skip if it looks like a header label or contains HTML remnants
            if not text or text in ("Leadership", "Lightspeed Team") or len(text) > 120:
                continue
            # Must contain at least one letter word (not just symbols)
            if re.search(r"[A-Za-z]{2,}", text):
                leadership.append(text)

    # --- Stage Invested ---
    # Take the first occurrence (could be LSVP or LSIP Investment section)
    stage = None
    m = re.search(r"Stage Invested.*?<dd>(.*?)</dd>", html, re.DOTALL)
    if m:
        stage = re.sub(r"<[^>]+>", "", m.group(1)).strip() or None

    # --- Investment Year ---
    inv_year = None
    m = re.search(r"(?:LSVP|LSIP) Investment.*?<dd>(\d{4})</dd>", html, re.DOTALL)
    if m:
        inv_year = m.group(1)

    # --- Description ---
    description = None
    m = re.search(r'<div[^>]+class="desc"[^>]*>\s*<p[^>]*>(.*?)</p>', html, re.DOTALL)
    if m:
        raw = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        # unescape common HTML entities
        raw = raw.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">")
        description = raw or None

    # --- Real emails in the page ---
    real_emails = _EMAIL_RE.findall(html)
    # Filter out site-infrastructure emails (wp, noreply, etc.)
    real_emails = [
        e for e in real_emails
        if not re.search(r"(wordpress|noreply|example|sentry|lsvp\.com|lsip\.com)", e, re.IGNORECASE)
    ]

    # --- LinkedIn URLs ---
    linkedin_urls = _LINKEDIN_RE.findall(html)

    return {
        "website": website,
        "leadership": leadership,
        "stage": stage,
        "investment_year": inv_year,
        "description": description,
        "real_emails": list(dict.fromkeys(real_emails)),  # dedupe, preserve order
        "linkedin_urls": list(dict.fromkeys(linkedin_urls)),
    }


def main() -> int:
    print("Fetching Lightspeed India portfolio page...")
    portfolio = _fetch_portfolio_companies()
    print(f"  {len(portfolio)} India-focused companies found\n")

    counts = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_website": 0,
        "skipped_bad_domain": 0,
    }
    seen_emails: set[str] = set()

    db = SessionLocal()
    try:
        for idx, (company_name, detail_url, _investor_type) in enumerate(portfolio, 1):
            print(f"[{idx:3d}/{len(portfolio)}] {company_name}")

            detail = _scrape_detail(detail_url)
            # Be polite to the server
            time.sleep(0.4)

            website = detail.get("website")
            if not website:
                print(f"         SKIP — no website found")
                counts["skipped_no_website"] += 1
                continue

            domain = _parse_domain(website)
            if not domain:
                print(f"         SKIP — bad domain: {website}")
                counts["skipped_bad_domain"] += 1
                continue

            stage = detail.get("stage")
            description = detail.get("description")
            leadership = detail.get("leadership", [])
            real_emails = detail.get("real_emails", [])
            linkedin_urls = detail.get("linkedin_urls", [])

            print(f"         domain={domain}  leaders={len(leadership)}  real_emails={len(real_emails)}")

            # --- Upsert Company ---
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
                db.flush()
                counts["companies_created"] += 1
            else:
                counts["companies_existing"] += 1

            # --- Contacts from leadership ---
            if not leadership:
                continue

            for entry in leadership:
                full_name = _parse_name(entry)
                if not full_name:
                    continue

                first = _first_name(full_name)
                if not first:
                    continue

                # 1. Try to find a real email in the page that matches this person
                real_email = None
                for candidate in real_emails:
                    if candidate.split("@")[0].lower() == first:
                        real_email = candidate.lower()
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

                # Dedup
                if email in seen_emails:
                    counts["contacts_existing"] += 1
                    continue
                existing = db.scalar(select(Contact).where(Contact.email == email))
                if existing is not None:
                    seen_emails.add(email)
                    counts["contacts_existing"] += 1
                    continue
                seen_emails.add(email)

                # Best LinkedIn URL (first one found, if any)
                linkedin_url = linkedin_urls[0] if linkedin_urls else None

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
