"""Scrape Stellaris Venture Partners' portfolio via static HTML.

Strategy:
  1. Fetch /portfolio page to collect all portfolio slugs (via JSON-LD and href links).
  2. Fetch each /portfolio/[slug] page and extract structured data from JSON-LD
     and HTML (founders with LinkedIn URLs, website URL, sector, stage,
     partnership year, co-investors, Stellaris team lead).
  3. Upsert Company + Contact rows into the pool.

Email priority for each founder:
  1. Real email found in HTML → email_verified=True, email_confidence=95
  2. Guessed firstname@domain → email_verified=False, email_confidence=60,
     scraped_pattern="firstname"

LinkedIn URLs are extracted from per-founder <a href="...linkedin.com/in/..."> links.

Usage:
    .venv/bin/python -m scripts.scrape_stellaris_venture_partners

Idempotent — re-runs skip existing emails / domains.
"""
from __future__ import annotations

import re
import sys
import time
import urllib.parse
import urllib.request
import json

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company, Contact

SOURCE_TAG = "stellaris-venture-partners-scraping"
BASE_URL = "https://www.stellarisvp.com"
PORTFOLIO_URL = f"{BASE_URL}/portfolio"

EMAIL_RE = re.compile(r"[w.+\-]+@[w\-]+\.[a-z]{2,}", re.IGNORECASE)
LINKEDIN_RE = re.compile(r'href=["\']([^"\']*linkedin\.com/in/[^"\']+)["\']', re.IGNORECASE)


# ---------------------------------------------------------------------------
# HTTP fetch helper
# ---------------------------------------------------------------------------

def _fetch_html(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    # Decode bytes; ignore surrogate chars gracefully
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Portfolio slug discovery
# ---------------------------------------------------------------------------

def _collect_slugs(html: str) -> list[str]:
    """Return deduplicated list of portfolio slugs from the /portfolio listing."""
    # Primary: href="/portfolio/<slug>" pattern
    hrefs = re.findall(r'["\']\/portfolio\/([a-z0-9][a-z0-9\-]+)["\']', html)
    seen: dict[str, None] = {}
    for slug in hrefs:
        if slug and slug not in seen:
            seen[slug] = None
    return list(seen.keys())


# ---------------------------------------------------------------------------
# Per-company page parsing
# ---------------------------------------------------------------------------

def _parse_jsonld(html: str) -> dict:
    """Extract the ProfilePage JSON-LD block from a company page."""
    blocks = re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    for block in blocks:
        # Replace control characters that break JSON parsing
        cleaned = re.sub(r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f]", " ", block)
        try:
            d = json.loads(cleaned)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("@type") == "ProfilePage":
            return d
    return {}


def _extract_founders_with_linkedin(html: str) -> list[tuple[str, str | None]]:
    """
    Return list of (founder_name, linkedin_url_or_None) from founder item blocks.

    Each block looks like:
      <div class="company_founder-item ...">
        <div class="text-size-medium">Founder Name</div>
        ...
        <a href="https://www.linkedin.com/in/slug/" ...>
    """
    result: list[tuple[str, str | None]] = []
    # Split on the founder-item class marker
    chunks = re.split(r'class="company_founder-item[^"]*"', html)
    for chunk in chunks[1:]:  # skip everything before first founder block
        # Find name: first text-size-medium div text
        name_match = re.search(r'class="text-size-medium">([^<]{1,120})</div>', chunk)
        if not name_match:
            continue
        name = name_match.group(1).strip()
        if not name:
            continue

        # Find LinkedIn href within this chunk (up to next founder-item or ~2000 chars)
        section = chunk[:2000]
        li_match = re.search(r'href=["\']([^"\']*linkedin\.com/in/[^"\']+)["\']', section, re.IGNORECASE)
        linkedin_url = li_match.group(1).strip() if li_match else None
        # Normalise linkedin URL
        if linkedin_url and not linkedin_url.startswith("http"):
            linkedin_url = "https://" + linkedin_url.lstrip("/")

        result.append((name, linkedin_url))
    return result


def _extract_field_value(html: str, label_pattern: str) -> str | None:
    """
    Find a label like 'PARTNERED' or 'entry STAGE' in the Webflow HTML and
    return the following company_details-text value.

    The pattern is:
      <div ...>LABEL</div>
      ...
      <div class="company_details-text">VALUE</div>
    """
    label_re = re.compile(
        r'(?:' + label_pattern + r').*?<div[^>]*class="company_details-text"[^>]*>(.*?)</div>',
        re.IGNORECASE | re.DOTALL,
    )
    m = label_re.search(html)
    if not m:
        return None
    raw = m.group(1)
    # Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _extract_co_investors(html: str) -> str | None:
    """Extract co-investor names from the CO-INVESTOR section."""
    # The section looks like:
    #   <div ...>CO-INVESTOR</div>...<div class="company_details-text">A\nB\nC</div>
    idx = html.upper().find("CO-INVESTOR")
    if idx == -1:
        return None
    section = html[idx : idx + 600]
    m = re.search(r'class="company_details-text"[^>]*>(.*?)</div>', section, re.DOTALL)
    if not m:
        return None
    raw = re.sub(r"<[^>]+>", " ", m.group(1))
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw or None


def _extract_stellaris_team(html: str) -> str | None:
    """Extract Stellaris team member name(s) from the STELLARIS TEAM section."""
    idx = html.upper().find("STELLARIS TEAM")
    if idx == -1:
        return None
    section = html[idx : idx + 800]
    # Look for <a href="/team/...">Name</a> links
    names = re.findall(r'href="/team/[^"]+">([^<]+)</a>', section)
    if names:
        return ", ".join(n.strip() for n in names)
    return None


def _parse_domain(website: str) -> str | None:
    """Return bare domain from a URL string."""
    if not website:
        return None
    if not website.startswith("http"):
        website = "http://" + website
    try:
        parsed = urllib.parse.urlparse(website)
        host = (parsed.netloc or parsed.path).lower()
        host = host.removeprefix("www.")
        host = host.split("/")[0].split("?")[0].split("#")[0]
        return host if "." in host else None
    except Exception:
        return None


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


def _extract_real_emails(html: str) -> list[str]:
    return [e.lower() for e in EMAIL_RE.findall(html)]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Fetching Stellaris Venture Partners portfolio listing...")
    portfolio_html = _fetch_html(PORTFOLIO_URL)
    slugs = _collect_slugs(portfolio_html)
    print(f"  {len(slugs)} portfolio slugs found: {slugs}")

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
        for i, slug in enumerate(slugs, 1):
            url = f"{BASE_URL}/portfolio/{slug}"
            print(f"  [{i}/{len(slugs)}] Fetching {url} ...")
            try:
                html = _fetch_html(url)
            except Exception as exc:
                print(f"    ERROR fetching {url}: {exc}")
                counts["skipped_no_website"] += 1
                continue

            # --- JSON-LD extraction ---
            ld = _parse_jsonld(html)
            about = ld.get("about", {}) if isinstance(ld, dict) else {}
            main_entity = ld.get("mainEntity", {}) if isinstance(ld, dict) else {}

            company_name = (about.get("name") or "").strip() or slug
            website = (about.get("url") or main_entity.get("url") or "").strip()
            description = (
                about.get("description") or main_entity.get("description") or ""
            ).strip() or None
            founding_date = (about.get("foundingDate") or "").strip() or None

            # investmentStage = entry stage; dateCreated = partnership year
            stage = (main_entity.get("investmentStage") or "").strip() or None
            partnership_year = (main_entity.get("dateCreated") or "").strip() or None
            sector = (main_entity.get("category") or "").strip() or None

            # Co-investors: from additionalProperty array or HTML
            co_investors: str | None = None
            for prop in main_entity.get("additionalProperty", []):
                if isinstance(prop, dict) and prop.get("name", "").lower() == "co-investors":
                    val = (prop.get("value") or "").strip()
                    if val:
                        # Normalise newlines to ", "
                        co_investors = re.sub(r"\s+", " ", val.replace("\n", ", "))
                    break
            if not co_investors:
                co_investors = _extract_co_investors(html)

            # Company status
            company_status: str | None = None
            for prop in main_entity.get("additionalProperty", []):
                if isinstance(prop, dict) and prop.get("name", "").lower() == "company status":
                    company_status = (prop.get("value") or "").strip() or None

            # Stellaris team lead
            stellaris_team = _extract_stellaris_team(html)

            # Website is required for domain derivation
            if not website:
                print(f"    No website found for {company_name}, skipping contacts")
                counts["skipped_no_website"] += 1
                # Still create the company with placeholder from slug
                domain = f"{slug.replace('-', '')}.com"
            else:
                domain = _parse_domain(website)

            if not domain:
                print(f"    Bad domain for {company_name} ({website}), skipping")
                counts["skipped_bad_domain"] += 1
                continue

            # --- Upsert Company ---
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
                print(f"    Created company: {company_name} ({domain})")
            else:
                counts["companies_existing"] += 1
                print(f"    Company exists: {company_name} ({domain})")

            # --- Extract founders from HTML (with individual LinkedIn URLs) ---
            founders_with_li = _extract_founders_with_linkedin(html)

            # Also fall back to JSON-LD founder field if HTML extraction found nothing
            if not founders_with_li:
                ld_founder = (about.get("founder") or "").strip()
                if ld_founder:
                    # Multiple founders concatenated with spaces/newlines — try to split
                    # Strategy: split on newlines first, then treat as single string
                    raw_names = re.split(r"\n", ld_founder)
                    if len(raw_names) == 1:
                        # May be "Firstname1 Lastname1 Firstname2 Lastname2" etc.
                        # No reliable split without knowing number of founders; keep as one
                        founders_with_li = [(ld_founder, None)]
                    else:
                        founders_with_li = [(n.strip(), None) for n in raw_names if n.strip()]

            # Real emails in HTML
            real_emails_in_html = set(_extract_real_emails(html))
            # Filter out Stellaris own domain emails and image/CDN noise
            real_emails_in_html = {
                e for e in real_emails_in_html
                if "stellarisvp" not in e
                and "website-files" not in e
                and "webflow" not in e
                and "@" in e
                and len(e) > 5
            }

            if not founders_with_li and not real_emails_in_html:
                counts["skipped_no_founders"] += 1
                print(f"    No founders found for {company_name}")
                # Polite rate-limit pause
                if i % 5 == 0:
                    time.sleep(0.5)
                continue

            # --- Handle real emails found in HTML (rare, but check) ---
            for email in sorted(real_emails_in_html):
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
                    name=company_name,
                    email=email,
                    role="Founder",
                    email_verified=True,
                    email_confidence=95,
                    scraped_pattern=None,
                    source=SOURCE_TAG,
                )
                db.add(contact)
                counts["contacts_created"] += 1

            # --- Handle per-founder contacts ---
            for full_name, linkedin_url in founders_with_li:
                first = _first_name(full_name)
                if not first or len(first) < 2:
                    continue

                # Only create email-guessed contacts if we have a real domain
                if not website:
                    # No domain available → LinkedIn-only contact (email=None)
                    # But our schema has email as nullable only if no UniqueConstraint issue
                    # We'll skip to avoid null email dupes
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
                    linkedin_url=linkedin_url,
                )
                db.add(contact)
                counts["contacts_created"] += 1
                print(f"      Contact: {full_name} <{email}>  linkedin={linkedin_url}")

            # Polite rate-limit pause every 5 companies
            if i % 5 == 0:
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
