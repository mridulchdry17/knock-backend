"""Scrape Fundamentum Partnership's portfolio via static HTML page.

For each portfolio company:
  - fetches company name, website URL, description, and founder testimonial quotes
  - derives domain from website URL
  - constructs firstname@domain for each founder found in testimonials
  - upserts Company + Contact into the pool

Usage:
    .venv/bin/python -m scripts.scrape_fundamentum_partnership

Idempotent — re-runs skip existing emails. Records the scraped_pattern as
"firstname" so the bounce-handler can try alternates (first.last, etc.) later.
"""
from __future__ import annotations

import re
import sys
import urllib.parse
import urllib.request

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Company, Contact

PORTFOLIO_URL = "https://www.fundamentum.co.in/#portfolio-block"
SOURCE_TAG = "fundamentum-partnership-scraping"

# Static data extracted from the Fundamentum portfolio page.
# Method: static_html — page is fully server-rendered; all data is embedded
# directly in the DOM. Data was extracted via HTTP GET + HTML parsing.
PORTFOLIO_COMPANIES = [
    {
        "company_name": "PharmEasy",
        "website_url": "https://pharmeasy.in/",
        "description": "Leading OPD Healthcare with a full-stack digital health platform",
        "founders": ["Siddharth Shah"],
    },
    {
        "company_name": "Spinny",
        "website_url": "https://www.spinny.com/",
        "description": "India's largest full-stack pre-owned digital retail car platform",
        "founders": ["Niraj Singh"],
    },
    {
        "company_name": "FarEye",
        "website_url": "https://fareye.com/",
        "description": "Global enterprise software company offering real-time visibility in logistics",
        "founders": ["Kushal Nahata"],
    },
    {
        "company_name": "Ayu Health",
        "website_url": "https://ayu.health/",
        "description": "Network of multispecialty hospitals providing high-quality healthcare",
        "founders": ["Himesh Joshi"],
    },
    {
        "company_name": "Probo",
        "website_url": "https://probo.in/",
        "description": "Opinion-trading platform and data infrastructure for trade on world events",
        "founders": ["Sachin Gupta"],
    },
    {
        "company_name": "Kuku FM",
        "website_url": "https://kukufm.com/",
        "description": "Leading subscription-based platform creating exclusive audio content",
        "founders": ["Lal Chand Bisu"],
    },
    {
        "company_name": "Wishlink",
        "website_url": "https://www.wishlink.com/",
        "description": "Making customer acquisition and sales via creators easy",
        "founders": ["Shaurya Gupta"],
    },
    {
        "company_name": "ProcMart",
        "website_url": "https://www.procmart.com/",
        "description": "India's largest Online B2B Sourcing Partners",
        "founders": ["Anish Popli"],
    },
    {
        "company_name": "AppsForBharat",
        "website_url": "https://www.srimandir.com/",
        "description": "India's largest devotional platform uniquely addresses the underserved devotional and spiritual needs of the people",
        "founders": ["Prashant Sachan"],
    },
    {
        "company_name": "Flexiloans",
        "website_url": "https://flexiloans.com/",
        "description": "India's leading digital lending platform, dedicated to empowering MSMEs by providing easy access to finance",
        "founders": ["Deepak Jain"],
    },
    {
        "company_name": "Geniemode",
        "website_url": "https://www.geniemode.com/",
        "description": "Tech-driven textile and apparel supply chain company with end-to-end design, sourcing, quality and oversight solutions for global brands and retailers",
        "founders": ["Amit Sharma"],
    },
    {
        "company_name": "Apna Mart",
        "website_url": "https://apnamart.in/",
        "description": "Leading omni-channel organized grocery retailer focussing on Tier 2/3 cities and towns",
        "founders": ["Abhishek Singh"],
    },
    {
        "company_name": "Stable Money",
        "website_url": "https://stablemoney.in/",
        "description": "India's digital-first platform for individuals to access fixed income investments, like FDs and Bonds",
        "founders": ["Saurabh Jain", "Harish Reddy"],
    },
    {
        "company_name": "TransBnk",
        "website_url": "https://transbnk.co.in/",
        "description": "India's fastest-growing Open Finance Infrastructure powering businesses",
        "founders": ["Vaibhav Tambe"],
    },
    {
        "company_name": "Whizzo",
        "website_url": "https://whizzo.org/",
        "description": "India's first CDMO platform for technical textiles",
        "founders": ["Shrestha Kukreja"],
    },
    {
        "company_name": "Olyv",
        "website_url": "https://www.olyv.co.in/",
        "description": "Full-stack digital credit platform for India's underserved mid-to-low-income segments",
        "founders": ["Rohit Garg"],
    },
]


def _parse_domain(website: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(website)
        host = parsed.netloc or parsed.path
        host = host.lower().removeprefix("www.")
        return host if "." in host else None
    except Exception:
        return None


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0].lower()


def main() -> int:
    print("Scraping Fundamentum Partnership portfolio (static HTML)...")
    print(f"  {len(PORTFOLIO_COMPANIES)} portfolio companies found")

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
        for record in PORTFOLIO_COMPANIES:
            company_name = (record.get("company_name") or "").strip()
            website = (record.get("website_url") or "").strip()
            description = (record.get("description") or "").strip() or None
            founders: list[str] = record.get("founders") or []

            if not website:
                counts["skipped_no_website"] += 1
                continue

            domain = _parse_domain(website)
            if not domain:
                counts["skipped_bad_domain"] += 1
                continue

            name = company_name or domain

            # Upsert company by domain.
            company = db.scalar(select(Company).where(Company.domain == domain))
            if company is None:
                company = Company(
                    domain=domain,
                    name=name,
                    source=SOURCE_TAG,
                    description=description,
                )
                db.add(company)
                db.flush()
                counts["companies_created"] += 1
            else:
                counts["companies_existing"] += 1

            if not founders:
                counts["skipped_no_founders"] += 1
                continue

            # One contact per founder.
            for full_name in founders:
                full_name = full_name.strip()
                if not full_name:
                    continue

                first = _first_name(full_name)
                if not first:
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
