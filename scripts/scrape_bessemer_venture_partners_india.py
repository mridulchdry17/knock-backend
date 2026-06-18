"""Scrape Bessemer Venture Partners India portfolio via static HTML parsing.

For each portfolio company:
  - fetches company name, website URL, BVP investor partner, founding year,
    partnered year, and sector/roadmap tags
  - derives domain from website URL
  - constructs firstname@domain for BVP investor partner as a best-guess contact
  - upserts Company + Contact into the pool

Usage:
    .venv/bin/python -m scripts.scrape_bessemer_venture_partners_india

Idempotent — re-runs skip existing emails. Records the scraped_pattern as
"firstname" so the bounce-handler can try alternates (first.last, etc.) later.

Notes:
  - WordPress site served via Netlify; all 42 portfolio companies are embedded
    as static HTML article blocks on the page — no JS rendering required.
  - Founder names are NOT present on this page; the BVP investor partner name
    is the best contact available (stored with role="Founder" as instructed).
  - 41/42 companies have a website URL; 41/42 have a BVP investor partner name.

HTML structure (per card):
  <article class="box investment with-overlay-on-open" id="companies-company-N">
    ...
    <div class="details ...">
      <h3 class="h-module-h3">Company Name</h3>
      <a class="cta button white" href="https://company.com/" ...>Visit Website</a>
      <div class="investors">
        <a class="team" href="...">Investor Name</a>
      </div>
      <div class="founded"><span class="year">2012</span></div>
      <div class="partnered"><span class="year">2013</span></div>
      <div class="roadmaps"><a class="roadmap ...">Tag</a> ...</div>
    </div>
  </article>
"""
from __future__ import annotations

import os
import re
import sys
import urllib.parse
import urllib.request

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company, Contact

PORTFOLIO_URL = "https://www.bvp.com/india"
SOURCE_TAG = "bessemer-venture-partners-india-scraping"


def _fetch_page() -> str:
    # Allow a local HTML cache to be used (set BVP_HTML_CACHE env var to file path)
    cache_path = os.environ.get("BVP_HTML_CACHE", "")
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8", errors="replace") as f:
            return f.read()
    req = urllib.request.Request(
        PORTFOLIO_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_domain(website: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(website)
        host = parsed.netloc or parsed.path
        host = host.lower().removeprefix("www.")
        host = host.split("/")[0]
        return host if "." in host else None
    except Exception:
        return None


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", _strip_tags(s)).strip()


def _scan_real_emails(text: str) -> list[str]:
    return re.findall(r"[\w.\+\-]+@[\w\-]+\.[a-z]{2,}", text, re.IGNORECASE)


def _scan_linkedin_urls(text: str) -> list[str]:
    return re.findall(r'https?://(?:www\.)?linkedin\.com/in/[^\s"\'<>]+', text)


def _parse_companies(html: str) -> list[dict]:
    """
    Parse all investment article cards from the BVP India page.

    Each card has id="companies-company-N" and class containing "investment".
    We extract the details overlay div which contains the structured fields.
    """
    companies: list[dict] = []

    # Match only article blocks that are investment cards
    article_re = re.compile(
        r'<article[^>]+class="[^"]*\binvestment\b[^"]*"[^>]+id="companies-company-\d+"[^>]*>'
        r'(.*?)'
        r'</article>',
        re.DOTALL | re.IGNORECASE,
    )

    for m in article_re.finditer(html):
        blob = m.group(1)

        # ---- Company name ---- #
        # Located in <h3 class="h-module-h3">Name</h3> inside the details div
        name_m = re.search(
            r'<h3[^>]+class="[^"]*h-module-h3[^"]*"[^>]*>\s*(.*?)\s*</h3>',
            blob, re.DOTALL | re.IGNORECASE,
        )
        if not name_m:
            # fallback: any h3
            name_m = re.search(r'<h3[^>]*>\s*(.*?)\s*</h3>', blob, re.DOTALL | re.IGNORECASE)
        company_name = _clean(name_m.group(1)) if name_m else ""

        if not company_name:
            # try the alt text of the logo image
            alt_m = re.search(r'alt="([^"]+)\s+logo"', blob, re.IGNORECASE)
            company_name = alt_m.group(1).strip() if alt_m else ""

        if not company_name:
            continue

        # ---- Website URL ---- #
        # <a class="cta button white" href="URL">Visit Website</a>
        website = ""
        ws_m = re.search(
            r'<a[^>]+href="([^"]+)"[^>]*>\s*Visit Website\s*</a>',
            blob, re.IGNORECASE,
        )
        if ws_m:
            website = ws_m.group(1).strip()

        # ---- BVP Investor Partner name ---- #
        # <div class="investors"><h4>Investors</h4><a class="team" href="...">Name</a></div>
        partner_name = ""
        investors_m = re.search(
            r'<div[^>]+class="[^"]*\binvestors\b[^"]*"[^>]*>(.*?)</div>',
            blob, re.DOTALL | re.IGNORECASE,
        )
        if investors_m:
            team_m = re.search(
                r'<a[^>]+class="[^"]*\bteam\b[^"]*"[^>]*>([^<]+)</a>',
                investors_m.group(1), re.IGNORECASE,
            )
            if team_m:
                partner_name = team_m.group(1).strip()

        # ---- Founded year ---- #
        founded_year = None
        founded_m = re.search(
            r'<div[^>]+class="[^"]*\bfounded\b[^"]*"[^>]*>.*?<span[^>]+class="[^"]*\byear\b[^"]*"[^>]*>(\d{4})</span>',
            blob, re.DOTALL | re.IGNORECASE,
        )
        if founded_m:
            founded_year = int(founded_m.group(1))

        # ---- Partnered year ---- #
        partnered_year = None
        partnered_m = re.search(
            r'<div[^>]+class="[^"]*\bpartnered\b[^"]*"[^>]*>.*?<span[^>]+class="[^"]*\byear\b[^"]*"[^>]*>(\d{4})</span>',
            blob, re.DOTALL | re.IGNORECASE,
        )
        if partnered_m:
            partnered_year = int(partnered_m.group(1))

        # ---- Roadmap / sector tags ---- #
        # <a class="roadmap cta button white" href="...">Tag Name</a>
        # Exclude the "India" tag since that's the filter tag not the sector
        tags = re.findall(
            r'<(?:a|span)[^>]+class="[^"]*\broadmap\b[^"]*"[^>]*>([^<]+)</(?:a|span)>',
            blob, re.IGNORECASE,
        )
        tags = [t.strip() for t in tags if t.strip() and t.strip().lower() != "india"]

        # ---- Real emails in blob ---- #
        real_emails = _scan_real_emails(blob)

        # ---- LinkedIn URLs in blob ---- #
        linkedin_urls = _scan_linkedin_urls(blob)

        companies.append(
            {
                "name": company_name,
                "website": website,
                "partner_name": partner_name,
                "founded_year": founded_year,
                "partnered_year": partnered_year,
                "tags": tags,
                "real_emails": real_emails,
                "linkedin_urls": linkedin_urls,
            }
        )

    return companies


def main() -> int:
    print(f"Fetching BVP India portfolio from {PORTFOLIO_URL} ...")
    html = _fetch_page()
    print(f"  Page fetched — {len(html):,} bytes")

    companies = _parse_companies(html)
    print(f"  {len(companies)} company cards parsed")

    counts = {
        "companies_created": 0,
        "companies_existing": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "skipped_no_website": 0,
        "skipped_bad_domain": 0,
        "skipped_no_contact": 0,
    }
    seen_emails: set[str] = set()

    db = SessionLocal()
    try:
        for record in companies:
            company_name = record["name"]
            website = record["website"]
            partner_name = record["partner_name"]
            tags = record["tags"]
            real_emails = record["real_emails"]
            linkedin_urls = record["linkedin_urls"]

            if not website:
                counts["skipped_no_website"] += 1
                print(f"  [SKIP-no-website] {company_name}")
                continue

            domain = _parse_domain(website)
            if not domain:
                counts["skipped_bad_domain"] += 1
                print(f"  [SKIP-bad-domain] {company_name} ({website})")
                continue

            industry = tags[0] if tags else None
            description = (
                f"Partnered {record['partnered_year']}" if record["partnered_year"] else None
            )

            # ---- Upsert Company ---- #
            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    domain=domain,
                    name=company_name,
                    source=SOURCE_TAG,
                    industry=industry,
                    description=description,
                )
                db.add(company)
                db.flush()
                counts["companies_created"] += 1
                print(f"  [+company] {company_name} ({domain})")
            else:
                counts["companies_existing"] += 1
                print(f"  [=company] {company_name} ({domain})")

            # ---- Build Contact ---- #
            # Priority 1: real email found in page HTML
            if real_emails:
                email = real_emails[0].lower()
                email_verified = True
                email_confidence = 95
                scraped_pattern = None
                contact_name = partner_name or None
                linkedin_url = linkedin_urls[0] if linkedin_urls else None
            # Priority 2: guess from BVP partner name (firstname@domain)
            elif partner_name:
                first = _first_name(partner_name)
                if not first:
                    counts["skipped_no_contact"] += 1
                    continue
                email = f"{first}@{domain}"
                email_verified = False
                email_confidence = 60
                scraped_pattern = "firstname"
                contact_name = partner_name
                linkedin_url = linkedin_urls[0] if linkedin_urls else None
            # No contact data at all
            else:
                counts["skipped_no_contact"] += 1
                print(f"  [SKIP-no-contact] {company_name}")
                continue

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
                name=contact_name,
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
                f"    [+contact] {contact_name or email} -> {email}"
                + (" (verified)" if email_verified else " (guessed)")
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
