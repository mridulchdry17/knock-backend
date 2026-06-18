# Knock Scraper — Email Guessing & Bounce Retry

How Knock discovers, guesses, and recovers email addresses for scraped contacts.

---

## 1. How scraped contacts enter the pool

The scraper pipeline (e.g. `scripts/scrape_elevation_capital.py`) hits a VC fund's public data source, extracts portfolio companies + founder names, and inserts them into the DB.

For each founder it:
1. Derives the company domain from the website URL (`sensehawk.com`)
2. Constructs the **first-guess email**: `firstname@domain` → `swarup@sensehawk.com`
3. Inserts a `Contact` row with `scraped_pattern="firstname"`, `email_verified=False`, `is_invalid=False`
4. Inserts (or skips if exists) a `Company` row with domain, name, stage, industry

**Multiple founders → multiple contact rows, one company row.**

```
companies:  id=42  domain=sensehawk.com  name=Sensehawk

contacts:   id=101  company_id=42  name=Swarup Mavanoor  email=swarup@sensehawk.com  scraped_pattern=firstname
            id=102  company_id=42  name=Rahul Sankhe     email=rahul@sensehawk.com   scraped_pattern=firstname
```

---

## 2. How the send works for multi-founder companies

The picker (`app/services/today_picker.py`) groups all contacts by `company_id` and sends **one email per company**:

- **TO** → one founder (randomly picked from the group)
- **CC** → remaining founders (up to 4, max 5 total recipients)

So Sensehawk sends as:
```
TO:  swarup@sensehawk.com
CC:  rahul@sensehawk.com
```

This counts as **1 send** against the daily cap, not 2.

---

## 3. Email guess pattern order

Defined in `app/services/email_patterns.py`:

| # | Pattern | Example |
|---|---|---|
| 1 | `firstname` | `swarup@sensehawk.com` ← **first guess (scraper starts here)** |
| 2 | `firstname.lastname` | `swarup.mavanoor@sensehawk.com` ← **second guess on bounce** |
| 3 | `f.lastname` | `s.mavanoor@sensehawk.com` |
| 4 | `firstnamelastname` | `swarupmavanoor@sensehawk.com` |
| 5 | `firstname_lastname` | `swarup_mavanoor@sensehawk.com` |
| 6 | `flastname` | `smavanoor@sensehawk.com` |
| 7 | `lastname` | `mavanoor@sensehawk.com` |

`firstname@` is first because it's the dominant pattern at VC-backed startups. `firstname.lastname@` is second because it's the most common corporate fallback.

---

## 4. What happens when an email bounces

Gmail delivers a bounce as a reply on the original thread. The reply ingestor (`app/services/reply_ingestor.py`) detects `is_bounce=True` and calls `_handle_bounce`.

**For scraped contacts** (`scraped_pattern` is set):

```
bounce detected
  → next_guess(name, domain, current_pattern)
      → finds next buildable pattern in EMAIL_PATTERN_ORDER
  → if next pattern found:
      contact.email = next_email          # e.g. swarup.mavanoor@sensehawk.com
      contact.scraped_pattern = next_pattern
      contact.is_invalid = False          # stays in rotation
  → if all patterns exhausted:
      contact.is_invalid = True
      contact.invalid_reason = "bounce_patterns_exhausted"
```

**For CSV/manual contacts** (no `scraped_pattern`):

```
bounce detected → contact.is_invalid = True, invalid_reason = "bounce"
```

Manual contacts are assumed to be real verified addresses, so a bounce means the address is dead — no retry.

---

## 5. Bounce scenarios for multi-founder companies

| Scenario | What happens |
|---|---|
| Both emails correct | Email delivered to both. Done. ✅ |
| TO correct, CC wrong | Email delivered. Bounce comes back on thread. System only advances TO contact's pattern — CC contact is not updated. ⚠️ Known gap. |
| TO wrong, CC correct | TO pattern advanced to next guess. CC contact stays in pool. Next pick: CC may become TO and deliver. ✅ |
| Both wrong | TO pattern advanced. CC stays as-is (still `is_invalid=False`). Next pick: CC becomes TO, bounces, CC pattern advances. Slow but eventually both exhaust correctly. ✅ |

### Known gap — CC bounce misattributed to TO contact

When the CC address bounces but TO is valid, the bounce notification comes back on the same thread. `_handle_bounce` reads `send_queue.to_contact_id` and advances the **TO** contact's pattern — even though the TO address was fine. This is a known bug; the fix requires parsing which address the bounce was for from the Gmail MAILER-DAEMON message body.

---

## 6. Adding a new VC fund scraper

1. Find the fund's portfolio page
2. Check if they use a public CMS API (Sanity, Contentful, etc.) — inspect image CDN URLs in source for project IDs
3. If CMS API available: query it directly (no Playwright needed)
4. If JS-rendered with no API: use Playwright to scrape the portfolio cards
5. Copy `scripts/scrape_elevation_capital.py` as the template
6. Change: `SANITY_PROJECT`, `SANITY_DATASET`, `GROQ_QUERY`, `SOURCE_TAG`
7. Run: `.venv/bin/python -m scripts.scrape_<fund_name>`

Script is fully idempotent — safe to re-run, skips existing emails/domains.

---

## 7. Source tags

| Source | What it means |
|---|---|
| `elevation-capital-scraping` | Elevation Capital VC portfolio — founders |
| `hr_seed_2026-06-11` | Manual HR contacts CSV upload |

Filter by source in admin to audit or bulk-delete a specific scrape batch.
