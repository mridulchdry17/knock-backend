"""Scrape Kalaari Capital's portfolio via their public WordPress site.

Strategy:
  1. Parse the portfolio index page (https://kalaari.com/portfolio/) for all
     /portfolio/[slug]/ hrefs — each represents an active portfolio company.
  2. Fetch each detail page and extract:
       - company name (from <title> tag)
       - website URL (from "Website" button)
       - founder names + LinkedIn URLs (from h3.aio-icon-title blocks under
         the "Founders" h2 heading)
       - investment stage (e.g. "Series A, 2019")
       - real email addresses (regex scan of full HTML)
  3. For each founder:
       a. If a real email is found in the HTML → email_verified=True, confidence=95
       b. Otherwise guess firstname@domain → email_verified=False, confidence=60,
          scraped_pattern="firstname"
  4. Upsert Company + Contact rows in the shared pool DB.

Usage:
    .venv/bin/python -m scripts.scrape_kalaari_capital

Idempotent — re-runs skip existing emails.
"""
from __future__ import annotations

import re
import sys
import time
import urllib.parse
import urllib.request
import html as html_lib

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company, Contact

SOURCE_TAG = "kalaari-capital-scraping"
INDEX_URL = "https://kalaari.com/portfolio/"
DETAIL_BASE = "https://kalaari.com/portfolio/"

# Polite crawl delay (seconds) between detail page fetches
CRAWL_DELAY = 0.5

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; KalaariScraper/1.0)"}

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# ─── HTML helpers ──────────────────────────────────────────────────────────────

def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _unescape(text: str) -> str:
    return html_lib.unescape(text).strip()


# ─── Index page ────────────────────────────────────────────────────────────────

def _fetch_slugs(html: str) -> list[str]:
    """Return unique company slugs from the portfolio index page."""
    raw = re.findall(
        r'href="https://kalaari\.com/portfolio/([^/"]+)/"',
        html,
    )
    return list(dict.fromkeys(raw))  # preserve order, deduplicate


# ─── Detail page parsers ───────────────────────────────────────────────────────

def _parse_company_name(html: str) -> str:
    """Extract company name from <title>CompanyName – Kalaari Capital</title>."""
    m = re.search(r"<title>([^<]+)</title>", html)
    if not m:
        return ""
    title = _unescape(m.group(1))
    # Strip " – Kalaari Capital" or " - Kalaari Capital" suffix
    title = re.sub(r"\s*[–\-]\s*Kalaari Capital.*$", "", title, flags=re.IGNORECASE)
    return title.strip()


def _parse_website(html: str) -> str | None:
    """Find the portfolio company's own website via the 'Website' button link."""
    # Pattern: href="https://..." ...>Website <i class=...
    m = re.search(
        r'href="(https?://(?!kalaari|fonts\.googleapis|fonts\.gstatic|gravatar)[^"]+)"'
        r"[^>]*>Website\s*<i class",
        html,
    )
    return m.group(1) if m else None


def _parse_stage(html: str) -> str | None:
    """Extract investment stage string like 'Series A, 2019' or 'Seed, 2021'."""
    m = re.search(
        r">((?:Series [A-Z\+]+|Pre-[Ss]eed|Seed|Growth)[^<]{0,40}?)</span>",
        html,
    )
    if m:
        return _unescape(m.group(1)).strip()
    return None


def _parse_founders(html: str) -> list[tuple[str, str | None]]:
    """
    Extract founder names and their LinkedIn URLs from the Founders section.

    Returns a list of (full_name, linkedin_url_or_None) tuples.
    """
    founders_idx = html.find("Founders</h2>")
    if founders_idx == -1:
        return []

    # Slice from the Founders heading to the next h2 or a reasonable cap
    section = html[founders_idx:]
    next_h2 = re.search(r"<h2[^>]*>", section[100:])
    if next_h2:
        section = section[: next_h2.start() + 100]
    else:
        section = section[:5000]

    # Founder name is in <h3 class="aio-icon-title ...">Name</h3>
    # LinkedIn is in <a href="https://...linkedin.com/in/...">LinkedIn</a> in the same block
    results: list[tuple[str, str | None]] = []

    # Split into individual founder blocks (each aio-icon-component div)
    blocks = re.split(r'<div class="aio-icon-component', section)
    for block in blocks[1:]:
        name_m = re.search(r'<h3 class="aio-icon-title[^"]*"[^>]*>\s*([^<]+)\s*</h3>', block)
        if not name_m:
            continue
        name = _unescape(name_m.group(1))
        if not name or re.fullmatch(r"[-–—N/Atbd.]+", name, re.IGNORECASE):
            continue

        li_m = re.search(
            r'href="(https?://(?:www\.|in\.)?linkedin\.com/in/[^"]+)"',
            block,
        )
        linkedin = li_m.group(1) if li_m else None
        results.append((name, linkedin))

    return results


def _parse_real_emails(html: str) -> set[str]:
    """Scan the HTML for any real email addresses (exclude Kalaari's own)."""
    found = set(EMAIL_RE.findall(html))
    # Exclude Kalaari's own domain and obvious false-positives from image filenames
    filtered = {
        e for e in found
        if "@kalaari." not in e
        and not e.endswith(".png")
        and not e.endswith(".jpg")
        and not e.endswith(".svg")
    }
    return filtered


# ─── Utilities ─────────────────────────────────────────────────────────────────

def _parse_domain(url: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.netloc or parsed.path).lower().removeprefix("www.")
        return host if "." in host else None
    except Exception:
        return None


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    print("Fetching Kalaari Capital portfolio index...")
    index_html = _fetch(INDEX_URL)
    slugs = _fetch_slugs(index_html)
    print(f"  {len(slugs)} company slugs found")

    counts: dict[str, int] = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_website": 0,
        "skipped_bad_domain": 0,
        "skipped_no_founders_no_email": 0,
    }
    seen_emails: set[str] = set()

    db = SessionLocal()
    try:
        for i, slug in enumerate(slugs, 1):
            detail_url = f"{DETAIL_BASE}{slug}/"
            try:
                detail_html = _fetch(detail_url)
            except Exception as exc:
                print(f"  [{i}/{len(slugs)}] SKIP {slug}: fetch error — {exc}")
                continue

            company_name = _parse_company_name(detail_html) or slug.replace("-", " ").title()
            website = _parse_website(detail_html)
            stage_raw = _parse_stage(detail_html)
            # Normalise stage to just the tier (drop year suffix)
            stage: str | None = None
            if stage_raw:
                stage = re.sub(r",\s*\d{4}.*$", "", stage_raw).strip()

            real_emails = _parse_real_emails(detail_html)
            founders = _parse_founders(detail_html)

            if not website:
                print(f"  [{i}/{len(slugs)}] SKIP {company_name}: no website found")
                counts["skipped_no_website"] += 1
                continue

            domain = _parse_domain(website)
            if not domain:
                print(f"  [{i}/{len(slugs)}] SKIP {company_name}: bad domain from {website}")
                counts["skipped_bad_domain"] += 1
                continue

            # ── Upsert company ──────────────────────────────────────────────
            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    domain=domain,
                    name=company_name,
                    source=SOURCE_TAG,
                    funding_stage=stage,
                    article_url=detail_url,
                )
                db.add(company)
                db.flush()  # obtain company.id before inserting contacts
                counts["companies_created"] += 1
                print(f"  [{i}/{len(slugs)}] NEW company: {company_name} ({domain})")
            else:
                counts["companies_existing"] += 1
                print(f"  [{i}/{len(slugs)}] EXISTS company: {company_name} ({domain})")

            # ── Contacts ────────────────────────────────────────────────────
            if not founders:
                # No structured founder section — try email-only contacts from the HTML
                if not real_emails:
                    counts["skipped_no_founders_no_email"] += 1
                    time.sleep(CRAWL_DELAY)
                    continue

            # Build a dict of first-name → real email (scoped to this domain) for
            # quick lookup when matching real emails to founders.
            domain_emails: dict[str, str] = {}
            for email in real_emails:
                local = email.split("@")[0].lower()
                domain_emails[local] = email

            if founders:
                for full_name, linkedin_url in founders:
                    first = _first_name(full_name)
                    if not first:
                        continue

                    # Priority 1: real email found in HTML at this domain
                    real_email: str | None = None
                    for local, email in domain_emails.items():
                        if local == first or email.split("@")[1].lower() == domain:
                            # Accept if local-part matches firstname or it's the only one
                            if local == first:
                                real_email = email
                                break
                    # Fallback: accept any domain-matching email if only one exists
                    if real_email is None:
                        same_domain = [e for e in real_emails if e.endswith(f"@{domain}")]
                        if len(same_domain) == 1:
                            real_email = same_domain[0]

                    if real_email:
                        email_addr = real_email.lower()
                        email_verified = True
                        email_confidence = 95
                        scraped_pattern = None
                    else:
                        # Priority 2: guess firstname@domain
                        email_addr = f"{first}@{domain}"
                        email_verified = False
                        email_confidence = 60
                        scraped_pattern = "firstname"

                    if email_addr in seen_emails:
                        counts["contacts_existing"] += 1
                        continue
                    existing = db.scalar(
                        select(Contact).where(Contact.email == email_addr)
                    )
                    if existing is not None:
                        counts["contacts_existing"] += 1
                        seen_emails.add(email_addr)
                        continue

                    seen_emails.add(email_addr)
                    contact = Contact(
                        company_id=company.id,
                        name=full_name,
                        email=email_addr,
                        role="Founder",
                        email_verified=email_verified,
                        email_confidence=email_confidence,
                        scraped_pattern=scraped_pattern,
                        linkedin_url=linkedin_url,
                        source=SOURCE_TAG,
                    )
                    db.add(contact)
                    counts["contacts_created"] += 1
            else:
                # No named founders — create contacts from real domain emails if found
                same_domain_emails = [e for e in real_emails if e.endswith(f"@{domain}")]
                for email_addr in same_domain_emails:
                    email_addr = email_addr.lower()
                    if email_addr in seen_emails:
                        counts["contacts_existing"] += 1
                        continue
                    existing = db.scalar(
                        select(Contact).where(Contact.email == email_addr)
                    )
                    if existing is not None:
                        counts["contacts_existing"] += 1
                        seen_emails.add(email_addr)
                        continue
                    seen_emails.add(email_addr)
                    contact = Contact(
                        company_id=company.id,
                        name=None,
                        email=email_addr,
                        role="Founder",
                        email_verified=True,
                        email_confidence=95,
                        scraped_pattern=None,
                        source=SOURCE_TAG,
                    )
                    db.add(contact)
                    counts["contacts_created"] += 1

            time.sleep(CRAWL_DELAY)

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print("\n=== Scrape complete ===")
    for k, v in counts.items():
        print(f"  {k}: {v}")

    total_skipped = (
        counts["skipped_no_website"]
        + counts["skipped_bad_domain"]
        + counts["skipped_no_founders_no_email"]
    )
    print(f"  total_skipped: {total_skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
