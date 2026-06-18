"""Scrape Artha Venture Fund portfolio via the artha.vc public API.

For each AVF portfolio company:
  - fetches name, founders, website, sector, stage from api.artha.vc/api/v1/portfolio
  - derives domain from website URL
  - constructs firstname@domain for each founder (guessed)
  - upserts Company + Contact into the pool

Usage:
    .venv/bin/python -m scripts.scrape_artha_venture_fund

Idempotent — re-runs skip existing emails. Records the scraped_pattern as
"firstname" so the bounce-handler can try alternates (first.last, etc.) later.

Data source: https://api.artha.vc/api/v1/portfolio (the SPA backend that powers
artha.vc — returns all 116 portfolio companies across all funds with full metadata
including founder names and website URLs).

Fund filter: funds list contains "AVF I" or "AVF II" (both are Artha Venture Fund).
"""
from __future__ import annotations

import re
import sys
import urllib.parse
import urllib.request
import json

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company, Contact

API_URL = "https://api.artha.vc/api/v1/portfolio"
SOURCE_TAG = "artha-venture-fund-scraping"

# AVF I and AVF II are both Artha Venture Fund vintages
AVF_FUND_TAGS = {"AVF I", "AVF II"}


def _fetch_portfolio() -> list[dict]:
    req = urllib.request.Request(
        API_URL,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Origin": "https://artha.vc",
            "Referer": "https://artha.vc/",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data


def _is_avf(record: dict) -> bool:
    funds = record.get("funds") or []
    return any(f in AVF_FUND_TAGS for f in funds)


def _parse_domain(website: str) -> str | None:
    if not website or website.lower() in ("inactive", "active", "n/a", ""):
        return None
    try:
        # Add scheme if missing so urlparse works
        if not website.startswith(("http://", "https://")):
            website = "https://" + website
        parsed = urllib.parse.urlparse(website)
        host = parsed.netloc or parsed.path
        # Strip query string parameters (some URLs have tracking params)
        host = host.split("?")[0].split("/")[0]
        host = host.lower().removeprefix("www.")
        return host if "." in host else None
    except Exception:
        return None


def _parse_founders(founders_raw: list | str | None) -> list[str]:
    """Normalise founders to a list of individual full names."""
    if not founders_raw:
        return []
    if isinstance(founders_raw, list):
        names = []
        for item in founders_raw:
            if isinstance(item, str):
                names.append(item.strip())
            elif isinstance(item, dict):
                n = item.get("name", "").strip()
                if n:
                    names.append(n)
        return [n for n in names if n and not re.fullmatch(r"[-–—N/Atbd.]+", n, re.IGNORECASE)]
    # String fallback — split on comma/newline/and
    raw = str(founders_raw).strip()
    if re.fullmatch(r"[-–—N/Atbd.]+", raw, re.IGNORECASE):
        return []
    cleaned = re.sub(r"\s+and\s+", ", ", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"[\n;]+", ", ", cleaned)
    return [p.strip() for p in cleaned.split(",") if p.strip()]


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


def _scan_for_real_email(text: str) -> str | None:
    """Return first real email address found in text, or None."""
    m = re.search(r"[\w.+\-]+@[\w\-]+\.[a-z]{2,}", text, re.IGNORECASE)
    return m.group(0).lower() if m else None


def main() -> int:
    print("Fetching Artha Venture Fund portfolio from api.artha.vc...")
    all_records = _fetch_portfolio()
    avf_records = [r for r in all_records if _is_avf(r)]
    print(f"  {len(all_records)} total records returned")
    print(f"  {len(avf_records)} AVF (I + II) companies")

    counts = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_website": 0,
        "skipped_no_founders": 0,
        "skipped_bad_domain": 0,
    }
    seen_emails: set[str] = set()

    db = SessionLocal()
    try:
        for record in avf_records:
            company_name = (record.get("name") or "").strip()
            founders_raw = record.get("founders")
            website = (record.get("website") or "").strip()
            sector = record.get("sector") or record.get("industryRaw") or None
            stage = record.get("stage") or record.get("stageRaw") or None
            linkedin_url = record.get("linkedin") or None
            # linkedin field is sometimes just a name string, not a URL
            if linkedin_url and "linkedin.com" not in linkedin_url.lower():
                linkedin_url = None

            if not website or website.lower() in ("inactive", "active", "n/a"):
                counts["skipped_no_website"] += 1
                print(f"  [skip-no-website] {company_name}")
                continue

            domain = _parse_domain(website)
            if not domain:
                counts["skipped_bad_domain"] += 1
                print(f"  [skip-bad-domain] {company_name} | website={website!r}")
                continue

            founders = _parse_founders(founders_raw)
            if not founders:
                counts["skipped_no_founders"] += 1
                print(f"  [skip-no-founders] {company_name}")
                continue

            display_name = company_name or domain

            # Upsert company by domain
            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    domain=domain,
                    name=display_name,
                    source=SOURCE_TAG,
                    funding_stage=stage,
                    industry=sector,
                )
                db.add(company)
                db.flush()
                counts["companies_created"] += 1
                print(f"  [company+] {display_name} | {domain}")
            else:
                counts["companies_existing"] += 1
                print(f"  [company=] {display_name} | {domain}")

            # One contact per founder
            for full_name in founders:
                first = _first_name(full_name)
                if not first:
                    continue

                # Check full_name text for a real embedded email
                real_email = _scan_for_real_email(full_name)

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
                    continue
                seen_emails.add(email)

                # Assign linkedin_url if only one founder (otherwise it's ambiguous)
                contact_linkedin = linkedin_url if len(founders) == 1 else None

                contact = Contact(
                    company_id=company.id,
                    name=full_name,
                    email=email,
                    role="Founder",
                    email_verified=email_verified,
                    email_confidence=email_confidence,
                    scraped_pattern=scraped_pattern,
                    linkedin_url=contact_linkedin,
                    source=SOURCE_TAG,
                )
                db.add(contact)
                counts["contacts_created"] += 1
                print(
                    f"    [contact+] {full_name} | {email} | verified={email_verified}"
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
