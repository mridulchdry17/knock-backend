# Knock — Product Requirements Document (Build Spec)

**Project name:** Knock
**Codename / repo:** outreach-backend (Python package: `app/`)
**Version:** 1.0
**Status:** Ready to build
**Audience:** LLM / engineer implementing the MVP from scratch

> Naming note: throughout this document the product is **Knock**. The Python package directory is still `app/` (not renamed to keep imports stable); only branding strings (FastAPI title, README, systemd unit names, public-facing copy) use "Knock".

---

## 1. Product Overview

### 1.1 One-line description
A web app where authenticated users connect their personal Gmail (via Google OAuth), pick contacts from a daily-refreshed pool of startups, and send personalized cold emails with automated follow-ups — without two users ever emailing the same person.

### 1.1.1 Architecture split (this repo = backend only)
- **Frontend** — Next.js (App Router), hosted on **Vercel**, lives in a separate repo. All UI/rendering. Calls our JSON API over HTTPS.
- **Backend (this repo)** — **FastAPI** JSON API + APScheduler background jobs, hosted on a **1 GB-RAM VM** (Hetzner CX11). Owns: SQLite DB, OAuth tokens (encrypted), scheduled jobs, all Gmail API calls, all scrapers.
- **Communication** — JSON over HTTPS. Cross-origin (frontend on Vercel → backend on `api.<domain>`). Auth strategy in §16. CORS strictly allow-listed.

### 1.2 Core problem
Job seekers, founders, and BD reps doing outbound cold email face three failures:
1. **Deliverability** — sending from a server gets blacklisted; sending from their own Gmail works but is unmanaged.
2. **Duplication** — multiple users in the same circle (e.g. job-seeking peers) end up emailing the same hiring manager, hurting everyone's reputation.
3. **Manual labor** — finding companies, finding emails, drafting, tracking, following up is a 5-tab manual process.

### 1.3 Solution
A multi-tenant SaaS that:
- Uses each user's own Gmail (via direct Google OAuth + Gmail API) → zero deliverability cost, real inbox reputation.
- Maintains a shared contact pool with a **global 2-day lock**: when user A emails contact X, no other user can email X for 2 days.
- Automates the pipeline: scraped company list → contact discovery → templated send → reply/bounce tracking → follow-up.

### 1.4 The moat
**Multi-user coordination on a shared contact pool.** Every other piece (scraping, sending, templating) is commodity. The 2-day cross-user lock is the defensible primitive.

---

## 2. Goals & Non-Goals

### 2.1 In scope (MVP, 4 weeks)
- Google login + Gmail connect (direct Google OAuth, our own OAuth client).
- Daily RSS-based company ingestion (TechCrunch, YourStory, Inc42, Google News).
- DIY contact email discovery (permutation + MX validation).
- Per-user templates with `{{name}}`, `{{company}}`, `{{role}}` placeholders.
- Campaign creation with pre-send moat filter (skip already-contacted, skip globally-locked).
- Send queue with random jitter, daily-limit enforcement, sending via Gmail API.
- Reply detection (Gmail thread polling via Gmail API).
- Bounce detection (mailer-daemon parsing).
- Follow-up automation (1 follow-up after 4 days, max 2 follow-ups, in-thread).
- User dashboard (sent/replied/bounced counts, contact-level status).
- Admin panel (browse data, mark invalid, trigger scrapers, global stats).
- Mandatory unsubscribe footer + auto-handling of "unsubscribe" replies.

### 2.2 Out of scope (MVP)
- LinkedIn scraping (legal risk; use manual CSV upload instead).
- Paid email-finder API integration (Hunter etc.) — wire it as a stub, integrate post-MVP.
- Billing / paywall.
- A/B testing of templates.
- AI-generated personalization.
- Mobile app.
- Team accounts (a single user is the unit; no orgs/seats).
- Outlook / non-Gmail providers.
- SMS / LinkedIn outreach channels.

### 2.3 Success criteria
- 5 beta users complete onboarding → send → receive reply, end-to-end.
- Bounce rate < 15%.
- Zero cross-user duplicate sends to the same contact within 2 days.
- Zero emails sent without an unsubscribe footer.
- Median time from "user signs up" to "first email sent" < 10 minutes.

---

## 3. User Personas & Core Flows

### 3.1 Persona: Aarav, 22, final-year student looking for a job
- Connects his Gmail.
- Picks 30 freshly-funded startups from the dashboard.
- Picks "applying for SDE role" template.
- App finds founder/CTO emails, queues sends, drips them out over the day.
- Gets 2 replies on day 3, 1 follow-up auto-sent on day 5.

### 3.2 Persona: Priya, 29, founder doing customer discovery
- Same as above but with a "we're building X for Y, would love 15 min" template.

### 3.3 Critical user journey (golden path)
1. Visit landing page → click "Connect Gmail".
2. Google OAuth consent → grant Gmail permissions → redirected back.
3. Dashboard shows: "Hi Aarav, Gmail connected." + "Browse Companies" CTA.
4. Browse companies, filter by stage/industry.
5. Click "Find contact" → enter "Rahul Sharma, CTO" → app suggests `rahul@acme.com` (MX-verified).
6. Save contact. Repeat for ~20 contacts.
7. Go to Templates → pick a starter template → tweak → save.
8. Go to Campaigns → "New campaign" → select template + 20 contacts → "Launch".
9. App responds: "Queued: 17. Skipped: 3 (2 already contacted by you, 1 locked by another user)."
10. Over the next 6 hours, 17 emails go out (random jitter, max 20/day).
11. Day 3: dashboard shows "1 reply, 0 bounces, 16 awaiting".
12. Day 5: follow-ups auto-sent for 15 unreplied (1 reply skipped).
13. Day 9: campaign closes; report card shown.

---

## 4. System Architecture

### 4.1 Diagram

```
┌────────────┐
│   User     │  Browser
└─────┬──────┘
      │ HTTPS
      ▼
┌────────────────────────────┐
│  Next.js (Vercel)          │   ← separate repo
│  - SSR/CSR pages           │
│  - calls our JSON API      │
└──────────┬─────────────────┘
           │ HTTPS (CORS-allowed)
           ▼
┌─────────────────────────────────┐
│  FastAPI App (1GB VM)           │   ← THIS REPO
│  ┌───────────────────────────┐  │
│  │ Routers (auth, campaigns, │  │
│  │ contacts, templates,      │  │
│  │ companies, admin)         │  │
│  └─────────┬─────────────────┘  │
│            │                    │
│  ┌─────────▼─────────────────┐  │
│  │ Services                  │  │
│  │  - google_client          │  │
│  │  - email_sender           │  │
│  │  - contact_finder         │  │
│  │  - reply_detector         │  │
│  │  - bounce_detector        │  │
│  └─────────┬─────────────────┘  │
│            │                    │
│  ┌─────────▼─────────────────┐  │
│  │ SQLite (single file)      │  │
│  └───────────────────────────┘  │
│                                 │
│  ┌───────────────────────────┐  │
│  │ APScheduler (in-process)  │  │
│  │  - send_worker (1 min)    │  │
│  │  - reply_check (30 min)   │  │
│  │  - bounce_check (30 min)  │  │
│  │  - followup_queue (daily) │  │
│  │  - daily_scrape (9 AM)    │  │
│  │  - daily_reset (midnight) │  │
│  └───────────────────────────┘  │
└──────────┬───────────┬──────────┘
           │           │
           ▼           ▼
   ┌──────────────┐  ┌──────────────────┐
   │ Google Gmail │  │ RSS Feeds        │
   │ API (OAuth2) │  │ (TechCrunch etc.)│
   │ user's inbox │  └──────────────────┘
   └──────────────┘
```

### 4.2 Architectural principles
- **Backend is a JSON API only.** No HTML rendering. Frontend (Next.js on Vercel) is a separate repo.
- **Single-process app.** FastAPI + APScheduler in one Python process. No Celery, no Redis, no queue server in MVP.
- **Server never sends mail directly.** All email I/O is HTTPS to Google's Gmail API. The VM's IP reputation is irrelevant.
- **RAM-conscious.** Target 1 GB VM. 1–2 uvicorn workers, threaded; lazy imports inside scrapers; SQLAlchemy with `expire_on_commit=False` and short sessions; no in-memory caches that grow unbounded.
- **SQLite is fine until ~50 concurrent users.** WAL mode enabled. Postgres migration is a Phase 2 concern, not MVP.
- **Idempotency everywhere a network call lives.** Every external call (Gmail send, RSS fetch, Gmail thread read) must be safely retryable.
- **The moat lives in two tables:** `user_contact_map` (per-user) and `global_contact_lock` (cross-user). Every send checks both.
- **Scheduler runs in worker 0 only.** With multi-worker uvicorn we gate APScheduler startup on `os.environ.get("RUN_SCHEDULER") == "1"` and run a separate systemd unit for it; alternatively single worker + threaded.

### 4.3 Why direct Google OAuth (decision locked-in)
- We own the OAuth client → no vendor in the data path, no per-call cost, full Gmail API surface.
- Tradeoff accepted: while in **unverified** state, OAuth consent shows "Google hasn't verified this app" and the app is capped at **100 users total** (Google's policy for restricted scopes like `gmail.send` / `gmail.readonly`).
- **Plan:** stay unverified for the entire MVP and beta (≤100 users). If we cross that threshold, submit for verification (~4–8 weeks) + pay for the third-party security assessment (~₹80k–₹2L, annual). Until then, ₹0 in OAuth/email costs.
- **Pre-flight:** create a Google Cloud project, enable Gmail API, configure OAuth consent screen as "External / Testing" first (lets us test with ≤100 listed test users), then publish to "External / Production" to allow any Gmail user to connect (still capped at 100 until verification).

---

## 5. Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Rich ecosystem for scraping + email |
| Web framework | **FastAPI 0.115+** | Async-friendly, Pydantic-native, fast, low overhead |
| ASGI server | uvicorn (with `--workers 1 --loop uvloop` on 1GB VM) | Standard FastAPI runtime |
| Validation | Pydantic v2 | Bundled with FastAPI; one source of truth for request/response shapes |
| ORM | SQLAlchemy 2.x (sync) | Sync is simpler + lower RAM than async on SQLite; we use `run_in_threadpool` for blocking ops inside async routes when needed |
| DB (MVP) | SQLite + WAL mode | Zero ops |
| DB (Phase 2) | Postgres 15 | Concurrency past ~50 users |
| Migrations | Alembic | Schema versioning from day 1 |
| Scheduler | APScheduler 3.x (`BackgroundScheduler`) | In-process cron, no broker |
| Auth (session) | Server-issued opaque session token in HTTP-only cookie (custom dependency) | Cross-origin to Vercel: cookie `SameSite=None; Secure; Domain=.<root-domain>`, validated server-side via `sessions` table |
| OAuth | google-auth + google-auth-oauthlib | Standard Google OAuth2 flow |
| Gmail | google-api-python-client | Official Gmail API client |
| Email rendering | Jinja2 (sandboxed env) | ONLY for rendering email bodies/subjects from user templates — no HTML UI |
| HTTP client | httpx | Async-capable; used for RSS + outbound REST |
| RSS parsing | feedparser | Battle-tested |
| HTML parsing | selectolax (lexbor) | ~5–10x lower RAM than beautifulsoup4+lxml; matters on 1GB |
| MX/DNS | dnspython | MX lookup |
| Email validation | email-validator | Syntax + MX |
| CORS | `fastapi.middleware.cors.CORSMiddleware` | Allow-list Vercel + custom-domain origins |
| Frontend | **Next.js (separate repo) on Vercel** | Owns all UI |
| Process manager | systemd | Standard Linux |
| Reverse proxy | Caddy | TLS via Let's Encrypt, auto-renew |
| Hosting | Hetzner CX11 / Azure B1s / any 1GB VM | Cheap |
| Logging | structlog → stdout | systemd captures to journald |
| Monitoring (MVP) | UptimeRobot ping `/healthz` | Free |

---

## 6. External Dependencies

### 6.1 Google OAuth + Gmail API (mandatory, MVP)
- **Purpose:** Authenticate the user (sign-in via Google) AND obtain Gmail send/read permission on their account.
- **OAuth client:** our own, created in Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client ID (type: Web application).
- **Authorized redirect URIs:** `http://localhost:5000/auth/google/callback` (dev) + `https://<prod-domain>/auth/google/callback`.
- **Scopes requested:**
  - `openid`
  - `https://www.googleapis.com/auth/userinfo.email`
  - `https://www.googleapis.com/auth/userinfo.profile`
  - `https://www.googleapis.com/auth/gmail.send` (RESTRICTED)
  - `https://www.googleapis.com/auth/gmail.readonly` (RESTRICTED — for reply + bounce detection)
- **OAuth consent screen:** External, Testing → Production once stable. Add brand info, privacy policy URL, terms URL, app domain. Add `gmail.send` and `gmail.readonly` to "Scopes" with justification text (see §22.2).
- **Token handling:**
  - On callback we receive `access_token` (1 hour TTL) + `refresh_token` (long-lived) + `id_token` (for user identity).
  - Persist `refresh_token` (encrypted at rest using Fernet — key in env `TOKEN_ENCRYPTION_KEY`).
  - Use `google.oauth2.credentials.Credentials` to materialize, auto-refresh as needed.
- **Gmail API calls used:**
  - `users().messages().send(userId='me', body=...)` → send (returns `id`, `threadId`).
  - `users().threads().get(userId='me', id=thread_id, format='metadata'|'full')` → reply detection.
  - `users().messages().list(userId='me', q='from:mailer-daemon newer_than:1d')` → bounce scan.
  - `users().messages().get(userId='me', id=msg_id, format='full')` → fetch bounce body to parse failed recipient.
- **Cap:** While unverified, Google enforces a **100-user lifetime cap** per OAuth client. Plan to submit for verification when approaching ~80 users.
- **Cost:** Gmail API itself is free. Cost is verification (~₹80k one-time + annual) once we want >100 users.

### 6.2 RSS feeds (mandatory, MVP)
- TechCrunch: `https://techcrunch.com/feed/`
- YourStory: `https://yourstory.com/feed`
- Inc42: `https://inc42.com/feed/`
- Google News (funding query): `https://news.google.com/rss/search?q=%22raises+seed%22+OR+%22series+A%22+India&hl=en-IN&gl=IN&ceid=IN:en`

All public RSS, no ToS issues, no scraping headers spoofing required.

### 6.3 Hunter.io (Phase 2, stubbed in MVP)
- Wired as a service interface; implementation returns `None` in MVP.
- Free tier: 25 searches/month — enough for dev.
- Add when DIY guess accuracy < 60%.

### 6.4 Things explicitly NOT used in MVP
- LinkedIn (manual CSV upload only).
- SendGrid / Postmark / SES — we never send via SMTP from our infra.
- Redis / RabbitMQ — APScheduler suffices.
- Stripe — no billing yet.

---

## 7. Database Schema (SQLite, full DDL)

> All tables. Copy-paste runnable. Use SQLAlchemy models that mirror this exactly.

```sql
-- =========================================================
-- 7.1 users
-- =========================================================
CREATE TABLE users (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    email                 TEXT    UNIQUE NOT NULL,
    full_name             TEXT,
    google_sub            TEXT    UNIQUE,              -- Google's stable user ID (from id_token 'sub')
    google_refresh_token  TEXT,                        -- encrypted with Fernet (TOKEN_ENCRYPTION_KEY)
    google_access_token   TEXT,                        -- encrypted; cached, refreshed as needed
    google_token_expiry   TIMESTAMP,                   -- when access_token expires
    google_scopes         TEXT,                        -- space-separated scopes granted
    google_connected_at   TIMESTAMP,
    daily_limit           INTEGER NOT NULL DEFAULT 20,
    sent_today            INTEGER NOT NULL DEFAULT 0,
    last_reset_date       DATE,
    sender_signature_name TEXT,                       -- shown in unsubscribe footer
    sender_signature_city TEXT,                       -- shown in unsubscribe footer
    is_admin              BOOLEAN NOT NULL DEFAULT 0,
    is_suspended          BOOLEAN NOT NULL DEFAULT 0,
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =========================================================
-- 7.2 companies (populated by scrapers + manual CSV)
-- =========================================================
CREATE TABLE companies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    domain        TEXT    UNIQUE NOT NULL,            -- canonical lowercase domain
    name          TEXT    NOT NULL,
    source        TEXT    NOT NULL,                   -- 'techcrunch'|'yourstory'|'inc42'|'gnews'|'manual'
    article_url   TEXT,
    funding_stage TEXT,                               -- 'pre_seed'|'seed'|'series_a'|...|null
    industry      TEXT,
    description   TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_companies_stage    ON companies(funding_stage);
CREATE INDEX idx_companies_industry ON companies(industry);

-- =========================================================
-- 7.3 contacts (a person at a company)
-- =========================================================
CREATE TABLE contacts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name                TEXT,
    role                TEXT,
    email               TEXT,                         -- nullable until found
    email_source        TEXT,                         -- 'guess'|'hunter'|'manual'
    email_confidence    INTEGER,                      -- 0..100
    email_verified      BOOLEAN NOT NULL DEFAULT 0,   -- MX/SMTP/Hunter verified
    is_invalid          BOOLEAN NOT NULL DEFAULT 0,   -- set after a hard bounce
    linkedin_url        TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(company_id, email)
);
CREATE INDEX idx_contacts_company ON contacts(company_id);
CREATE INDEX idx_contacts_email   ON contacts(email);

-- =========================================================
-- 7.4 user_contact_map (the per-user moat half)
-- =========================================================
-- One row per (user, contact). Tracks the lifecycle of
-- this user's outreach to this contact across initial + follow-ups.
CREATE TABLE user_contact_map (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    contact_id      INTEGER NOT NULL REFERENCES contacts(id),
    campaign_id     INTEGER REFERENCES campaigns(id),
    status          TEXT    NOT NULL,                 -- QUEUED|SENT|FOLLOWUP_SENT|REPLIED|BOUNCED|UNSUBSCRIBED|DEAD
    gmail_thread_id TEXT,
    gmail_message_id TEXT,                            -- first message
    sent_at         TIMESTAMP,
    last_followup_at TIMESTAMP,
    followup_count  INTEGER NOT NULL DEFAULT 0,
    reply_detected_at TIMESTAMP,
    bounce_detected_at TIMESTAMP,
    last_action_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, contact_id)
);
CREATE INDEX idx_ucm_user_status   ON user_contact_map(user_id, status);
CREATE INDEX idx_ucm_thread        ON user_contact_map(gmail_thread_id);
CREATE INDEX idx_ucm_followup_scan ON user_contact_map(status, sent_at, followup_count);

-- =========================================================
-- 7.5 global_contact_lock (the cross-user moat half)
-- =========================================================
CREATE TABLE global_contact_lock (
    contact_id        INTEGER PRIMARY KEY REFERENCES contacts(id),
    locked_by_user_id INTEGER NOT NULL REFERENCES users(id),
    locked_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    locked_until      TIMESTAMP NOT NULL              -- locked_at + 30 days
);
CREATE INDEX idx_lock_until ON global_contact_lock(locked_until);

-- =========================================================
-- 7.6 templates (per-user)
-- =========================================================
CREATE TABLE templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    name        TEXT    NOT NULL,
    subject     TEXT    NOT NULL,
    body        TEXT    NOT NULL,                     -- supports {{name}} {{company}} {{role}} {{first_name}}
    is_followup BOOLEAN NOT NULL DEFAULT 0,
    parent_template_id INTEGER REFERENCES templates(id),  -- if is_followup, points to initial
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =========================================================
-- 7.7 campaigns
-- =========================================================
CREATE TABLE campaigns (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    name                TEXT    NOT NULL,
    template_id         INTEGER NOT NULL REFERENCES templates(id),
    followup_template_id INTEGER REFERENCES templates(id),
    status              TEXT    NOT NULL DEFAULT 'DRAFT', -- DRAFT|RUNNING|COMPLETED|CANCELLED
    queued_count        INTEGER NOT NULL DEFAULT 0,
    skipped_count       INTEGER NOT NULL DEFAULT 0,
    sent_count          INTEGER NOT NULL DEFAULT 0,
    replied_count       INTEGER NOT NULL DEFAULT 0,
    bounced_count       INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at          TIMESTAMP,
    completed_at        TIMESTAMP
);

-- =========================================================
-- 7.8 send_queue (the work table)
-- =========================================================
CREATE TABLE send_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    contact_id      INTEGER NOT NULL REFERENCES contacts(id),
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id),
    template_id     INTEGER NOT NULL REFERENCES templates(id),
    kind            TEXT    NOT NULL,                 -- 'INITIAL' | 'FOLLOWUP'
    in_reply_to_thread_id TEXT,                       -- only for FOLLOWUP
    scheduled_for   TIMESTAMP NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'PENDING', -- PENDING|SENT|FAILED|SKIPPED
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sent_at         TIMESTAMP
);
CREATE INDEX idx_queue_due ON send_queue(status, scheduled_for);
CREATE INDEX idx_queue_user ON send_queue(user_id, status);

-- =========================================================
-- 7.9 email_logs (immutable audit trail)
-- =========================================================
CREATE TABLE email_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    contact_id  INTEGER REFERENCES contacts(id),
    action      TEXT    NOT NULL,                     -- 'sent'|'send_failed'|'reply_detected'|'bounce_detected'|'unsubscribe_detected'|'followup_queued'|'followup_sent'
    metadata    TEXT,                                 -- JSON blob (subject, error, thread_id, etc)
    timestamp   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_logs_user_time ON email_logs(user_id, timestamp);

-- =========================================================
-- 7.10 sessions (server-side opaque session tokens — required for cross-origin)
-- =========================================================
CREATE TABLE sessions (
    id           TEXT    PRIMARY KEY,                 -- random 32-byte urlsafe-b64; this is the cookie value
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at   TIMESTAMP NOT NULL,                  -- created_at + 30 days; sliding window via last_used_at
    user_agent   TEXT,
    ip           TEXT
);
CREATE INDEX idx_sessions_user    ON sessions(user_id);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);
```

### 7.11 Schema invariants (must hold at all times)
- For any `(user_id, contact_id)`, at most one `user_contact_map` row exists.
- A contact is "globally locked" iff a row in `global_contact_lock` has `locked_until > now()`.
- `email_logs` is append-only — never UPDATE or DELETE.
- `send_queue.status='SENT'` rows are kept (not deleted) for audit, but ignored by the worker.
- `users.sent_today` is reset to 0 by the daily-reset job at local midnight; if missed, the send worker also lazily resets when it sees `last_reset_date < today`.

---

## 8. Google OAuth + Gmail API Integration Spec

### 8.1 Google Cloud Console setup
1. Create a Google Cloud project (e.g. `knock-prod`).
2. APIs & Services → Library → enable **Gmail API**.
3. APIs & Services → OAuth consent screen:
   - User type: **External**.
   - Fill app name, user-support email, developer contact.
   - Add app domain, privacy policy URL, terms URL (required before publishing).
   - Add scopes: `openid`, `userinfo.email`, `userinfo.profile`, `gmail.send`, `gmail.readonly`.
   - For each restricted scope, write justification text (see §22.2).
   - During development, add yourself + beta testers as "Test users" (max 100).
   - Once stable, click **Publish App** → status moves from Testing → In Production. The 100-user lifetime cap still applies until verification is approved.
4. APIs & Services → Credentials → Create Credentials → **OAuth 2.0 Client ID**:
   - Application type: Web application.
   - Authorized JavaScript origins: `http://localhost:5000`, `https://<prod-domain>`.
   - Authorized redirect URIs: `http://localhost:5000/auth/google/callback`, `https://<prod-domain>/auth/google/callback`.
   - Download JSON. Extract `client_id` + `client_secret` into `.env`.

### 8.2 Token storage (security)
- `refresh_token` is the long-lived secret — encrypted at rest using **Fernet** symmetric encryption.
- Encryption key is `TOKEN_ENCRYPTION_KEY` in `.env` (a 32-byte base64 string from `Fernet.generate_key()`).
- `access_token` is short-lived (1h); we cache it but always check expiry before reuse.
- On token revocation by user (e.g. they remove our app from Google account permissions), the next API call returns 401 → we mark `user.is_suspended=True` and surface a "reconnect Gmail" CTA.

### 8.3 Wrapper module: `services/google_client.py`

Public surface (what the rest of the app calls):

```python
# services/google_client.py
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

def build_oauth_flow(redirect_uri: str, state: str | None = None) -> Flow:
    """Returns a Flow configured from settings.GOOGLE_CLIENT_ID / SECRET."""

def get_authorization_url(redirect_uri: str) -> tuple[str, str]:
    """Returns (auth_url, state). Always set access_type='offline' + prompt='consent'
    so we get a refresh_token even on re-auth."""

def exchange_code_for_tokens(code: str, redirect_uri: str, state: str) -> dict:
    """Returns {access_token, refresh_token, expiry, id_token, scopes, email, sub, name}."""

def credentials_for_user(user) -> Credentials:
    """Decrypt stored tokens, build google.oauth2.credentials.Credentials.
    If access_token expired and refresh_token present, refresh + persist back."""

def gmail_service(user):
    """Returns a Gmail API client: build('gmail', 'v1', credentials=credentials_for_user(user))."""

# --- High-level operations used by send_worker / detectors ---

def send_message(
    user,
    to: str,
    subject: str,
    body_html: str,
    body_text: str,
    in_reply_to_thread_id: str | None = None,
) -> dict:                                           # returns {"id": ..., "threadId": ...}
    """Builds a MIME message (multipart/alternative) and calls
       service.users().messages().send(userId='me', body={'raw': b64url(mime),
                                                           'threadId': thread_id?})."""

def fetch_thread(user, thread_id: str) -> dict:
    """service.users().threads().get(userId='me', id=thread_id, format='full').
    Returns parsed: {'messages': [{'from','date','snippet','headers'}]}."""

def search_messages(user, query: str, max_results: int = 50) -> list[dict]:
    """service.users().messages().list(userId='me', q=query, maxResults=...).
    Then .get(format='full') on each id to fetch bodies for bounce parsing."""

def get_user_profile(user_or_creds) -> dict:
    """userinfo.get → {email, sub, name, picture}."""
```

### 8.4 Building the MIME message (for send)

```python
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

def build_raw_message(from_addr, to, subject, body_html, body_text,
                      in_reply_to_message_id=None, references=None):
    msg = MIMEMultipart('alternative')
    msg['From']    = from_addr
    msg['To']      = to
    msg['Subject'] = subject
    if in_reply_to_message_id:
        msg['In-Reply-To'] = in_reply_to_message_id
        msg['References']  = references or in_reply_to_message_id
    msg.attach(MIMEText(body_text, 'plain'))
    msg.attach(MIMEText(body_html, 'html'))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return raw
```

For follow-ups, set `body['threadId'] = thread_id` AND set `In-Reply-To` / `References` headers using the original message's RFC 2822 `Message-Id` (fetched once and stored on the UCM row, optionally — Gmail will still thread correctly with just `threadId`, but the headers are good practice).

### 8.5 Error handling
All Google API calls wrapped with try/except for `HttpError`:
- **HTTP 401** (`invalid_grant`, token revoked) → set `user.is_suspended=True`, clear tokens, log, surface "reconnect Gmail" on dashboard.
- **HTTP 403** with reason `rateLimitExceeded` or `userRateLimitExceeded` → exponential backoff (60s, 5m, 30m) and re-queue.
- **HTTP 403** with reason `dailyLimitExceeded` → freeze user's queue for 24h, alert admin.
- **HTTP 400** (malformed request, e.g. bad email) → mark queue row `FAILED`, mark contact `is_invalid=True`.
- **HTTP 5xx / network** → 3 retries with exponential backoff; then `FAILED`.

### 8.6 Quota awareness (Gmail API)
- Per-user quota: 1,000,000,000 quota units/day (effectively unlimited at our scale).
- Per-user-per-second: 250 quota units. `messages.send` costs 100 units → ~2.5 sends/second/user max. Our jitter (2–10 min) is far below this.
- The real bottleneck is Gmail's anti-abuse layer (separate from API quota): a free Gmail account can practically send ~100/day, Workspace ~500/day, before reputation penalties kick in. Our `daily_limit=20` (hard ceiling 30) stays well under both.

---

## 9. Email Sending Pipeline (the heart)

### 9.1 Flow

```
User clicks "Launch Campaign"
   │
   ▼
campaigns.launch(campaign_id):
   for contact in campaign.selected_contacts:
       if not contact.email:                         → skip "no email"
       if contact.is_invalid:                         → skip "invalid email"
       if user.user_contact_map.exists(contact.id):  → skip "already contacted"
       if global_lock.is_locked(contact.id) and
          global_lock.locked_by != user.id:           → skip "locked by another"
       else:
           queue_initial_send(...)
   set campaign.status = RUNNING

(async) APScheduler send_worker (every 1 min):
   pending = SELECT * FROM send_queue
             WHERE status='PENDING' AND scheduled_for <= NOW()
             ORDER BY scheduled_for
             LIMIT 50
   for row in pending:
       lock row (UPDATE status='LOCKED' WHERE id=? AND status='PENDING')
       try send_one(row)
       except: row.status='PENDING'; row.attempts+=1; backoff
```

### 9.2 `send_one` implementation (pseudocode)

```python
def send_one(queue_row):
    user = User.get(queue_row.user_id)
    contact = Contact.get(queue_row.contact_id)
    template = Template.get(queue_row.template_id)

    # Reset daily counter lazily
    if user.last_reset_date != today():
        user.sent_today = 0
        user.last_reset_date = today()

    # Daily cap
    if user.sent_today >= user.daily_limit:
        queue_row.scheduled_for = tomorrow_at_random_time()
        queue_row.status = 'PENDING'
        return

    # Re-check moat (could have changed since queueing)
    lock = GlobalContactLock.get(contact.id)
    if lock and lock.locked_until > now() and lock.locked_by_user_id != user.id:
        queue_row.status = 'SKIPPED'
        log('skipped_locked')
        return

    # Render
    ctx = {
        'name': contact.name or 'there',
        'first_name': (contact.name or '').split()[0] or 'there',
        'company': contact.company.name,
        'role': contact.role or '',
    }
    subject = jinja_render(template.subject, ctx)
    body    = jinja_render(template.body,    ctx)
    body_with_footer = body + render_unsubscribe_footer(user)

    # Send
    try:
        result = google_client.send_message(
            user=user,
            to=contact.email,
            subject=subject,
            body_html=markdown_to_html(body_with_footer),
            body_text=body_with_footer,
            in_reply_to_thread_id=queue_row.in_reply_to_thread_id,
        )
        # result = {"id": gmail_message_id, "threadId": gmail_thread_id}
    except AuthError:                                  # 401 invalid_grant
        user.is_suspended = True
        user.google_refresh_token = None
        queue_row.status = 'FAILED'
        queue_row.last_error = 'auth'
        return
    except (RateLimitError, NetworkError) as e:
        queue_row.attempts += 1
        if queue_row.attempts >= 3:
            queue_row.status = 'FAILED'
            queue_row.last_error = str(e)
            ucm.status = 'DEAD'
            return
        queue_row.scheduled_for = now() + backoff(queue_row.attempts)
        return

    # Persist success
    queue_row.status = 'SENT'
    queue_row.sent_at = now()

    if queue_row.kind == 'INITIAL':
        UserContactMap.upsert(
            user_id=user.id, contact_id=contact.id,
            campaign_id=queue_row.campaign_id,
            status='SENT',
            gmail_thread_id=result['threadId'],
            gmail_message_id=result['id'],
            sent_at=now(),
        )
        GlobalContactLock.set(contact_id=contact.id,
                              locked_by=user.id,
                              locked_until=now()+30days)
    else:  # FOLLOWUP
        ucm = UserContactMap.get(user.id, contact.id)
        ucm.status = 'FOLLOWUP_SENT'
        ucm.followup_count += 1
        ucm.last_followup_at = now()
        # extend lock another 30 days
        GlobalContactLock.set(contact_id=contact.id,
                              locked_by=user.id,
                              locked_until=now()+30days)

    user.sent_today += 1
    EmailLog.create(user_id=user.id, contact_id=contact.id,
                    action='sent', metadata={'subject': subject,
                                             'thread_id': result['threadId']})

    # Random jitter for next send for this user (handled at queue time, not here)
```

### 9.3 Queue scheduling (jitter)
At queue time, each row's `scheduled_for` = `now() + random_minutes(2, 10) * row_index`, capped to keep within user's working hours (default 9 AM – 7 PM IST). Beyond cap → push to next day.

### 9.4 Daily limits
- Default `daily_limit = 20`. Configurable per-user (admin-only in MVP).
- Hard ceiling: 30. Never raise above this in MVP regardless of admin input.

### 9.5 Unsubscribe footer (mandatory)

```
---
You're receiving this because I think there might be mutual value here.
If you'd rather not hear from me, just reply with "unsubscribe" and I won't email you again.

{user.sender_signature_name}
{user.sender_signature_city}
```

If `sender_signature_name` is null, fall back to user's Google profile name, or block sending until they fill it in (Phase 1: block).

---

## 10. Reply, Bounce, Unsubscribe Detection

### 10.1 Reply detection (job: every 30 min)

```python
def reply_check_job():
    for ucm in UserContactMap.where(status__in=['SENT','FOLLOWUP_SENT'],
                                    reply_detected_at__isnull=True):
        user = User.get(ucm.user_id)
        if user.is_suspended or not user.google_refresh_token: continue

        thread = google_client.fetch_thread(user, ucm.gmail_thread_id)
        # thread = {'messages': [{'id','threadId','snippet','payload':{'headers':[...]}}, ...]}

        # Find any message NOT from the user
        for msg in thread['messages']:
            headers = {h['name'].lower(): h['value'] for h in msg['payload']['headers']}
            from_addr = parse_email_addr(headers.get('from', ''))
            internal_date = int(msg['internalDate']) / 1000  # ms → s

            if from_addr.lower() != user.email.lower():
                ucm.status = 'REPLIED'
                ucm.reply_detected_at = datetime.fromtimestamp(internal_date)
                EmailLog.create(user_id=user.id, contact_id=ucm.contact_id,
                                action='reply_detected',
                                metadata={'from': from_addr})
                # Cancel pending follow-ups
                SendQueue.update(
                    {'status': 'SKIPPED'},
                    where(user_id=user.id, contact_id=ucm.contact_id,
                          status='PENDING')
                )

                # Special-case: unsubscribe
                if 'unsubscribe' in msg['snippet'].lower():
                    ucm.status = 'UNSUBSCRIBED'
                    EmailLog.create(action='unsubscribe_detected', ...)
                break
```

### 10.2 Bounce detection (job: every 30 min, same job as reply or separate)

```python
def bounce_check_job():
    for user in User.where(is_suspended=False, google_refresh_token__notnull=True):
        # Search user's inbox for bounces from last 24 hours
        msgs = google_client.search_messages(
            user,
            query='from:mailer-daemon OR from:postmaster-noreply newer_than:1d',
            max_results=50,
        )
        for m in msgs:
            # m is the full message resource (use format='full' inside search_messages)
            body = extract_plain_body(m['payload'])
            failed_recipient = parse_bounce_recipient(body)  # regex below
            if not failed_recipient: continue

            contact = Contact.find_by_email(failed_recipient)
            if not contact: continue

            ucm = UserContactMap.get(user.id, contact.id)
            if not ucm or ucm.status == 'BOUNCED': continue

            ucm.status = 'BOUNCED'
            ucm.bounce_detected_at = now()
            contact.is_invalid = True
            contact.email_verified = False
            EmailLog.create(user_id=user.id, contact_id=contact.id,
                            action='bounce_detected')
            # Cancel pending followups
            SendQueue.update({'status':'SKIPPED'},
                where(user_id=user.id, contact_id=contact.id, status='PENDING'))
```

`parse_bounce_recipient` regex candidates (any match wins):
- `Final-Recipient:\s*rfc822;\s*([^\s]+)`
- `failed permanently[^\n]*<([^>]+)>`
- `delivery to the following recipient failed permanently:\s*([^\s]+)`

### 10.3 Unsubscribe handling
Triggered inside reply detection (above). On unsubscribe:
- `ucm.status = 'UNSUBSCRIBED'`.
- All pending `send_queue` rows for this `(user, contact)` → `SKIPPED`.
- Contact remains valid for *other* users (it's user-specific opt-out).
- A user-level table `user_unsubscribes(user_id, contact_id)` could be added to enforce permanent block; MVP just relies on `ucm` row existing → user_contact_map.exists() check during pre-send filters subsequent campaigns.

---

## 11. Follow-up Engine

### 11.1 Rules
- Only one follow-up template per campaign in MVP.
- Follow-up fires when **all** are true:
  - `ucm.status == 'SENT'`
  - `now() - ucm.sent_at >= 4 days` (configurable at campaign level)
  - `ucm.followup_count < 2`
  - `ucm.status NOT IN ('REPLIED','BOUNCED','UNSUBSCRIBED','DEAD')`

### 11.2 Job (daily, 10 AM)

```python
def followup_queue_job():
    candidates = UserContactMap.query(
        status='SENT',
        sent_at__lte=now() - timedelta(days=4),
        followup_count__lt=2,
    )
    for ucm in candidates:
        campaign = Campaign.get(ucm.campaign_id)
        if not campaign.followup_template_id:
            continue

        # Don't queue if already queued
        if SendQueue.exists(user_id=ucm.user_id,
                            contact_id=ucm.contact_id,
                            kind='FOLLOWUP', status='PENDING'):
            continue

        SendQueue.create(
            user_id=ucm.user_id,
            contact_id=ucm.contact_id,
            campaign_id=campaign.id,
            template_id=campaign.followup_template_id,
            kind='FOLLOWUP',
            in_reply_to_thread_id=ucm.gmail_thread_id,
            scheduled_for=jittered_time_today(),
        )
        EmailLog.create(action='followup_queued', ...)
```

### 11.3 In-thread reply
Follow-ups MUST be sent as a reply on the original `gmail_thread_id`, not a fresh thread. The Gmail API call sets `body['threadId'] = thread_id` and the MIME message sets `In-Reply-To` + `References` headers (see §8.4).

---

## 12. Company Ingestion (Scrapers)

### 12.1 Module: `scrapers/`

Each scraper exports:

```python
def fetch() -> list[CompanyCandidate]:
    """Returns parsed companies from this source."""

@dataclass
class CompanyCandidate:
    name: str
    domain: str | None
    article_url: str
    funding_stage: str | None
    industry: str | None
    description: str | None
    source: str
```

### 12.2 Per-source logic

**TechCrunch / YourStory / Inc42 / Google News:** all RSS-based, parsed via `feedparser`.

For each entry:
1. Title contains funding keywords (`raises`, `seed`, `series`, `funding`, `raised`)? If not, skip.
2. Extract company name: first proper noun before "raises" (regex), fallback to first capitalized phrase.
3. Extract domain:
   - First, look in entry summary/body for URLs that aren't the publisher's own domain.
   - If none, attempt to derive from name (slugify + `.com` / `.in`) — only if MX resolves; else skip.
4. Funding stage: regex on title (`pre-seed|seed|series A|series B|...`).
5. Industry: extract via simple keyword bucket (fintech/healthtech/saas/edtech/...) — else null.

### 12.3 Orchestrator (`scrapers/orchestrator.py`)

```python
def run_all_scrapers():
    total_added = 0
    for src in [techcrunch, yourstory, inc42, gnews]:
        try:
            candidates = src.fetch()
        except Exception as e:
            log.exception(f"{src.__name__} failed: {e}")
            continue
        for c in candidates:
            if not c.domain: continue
            try:
                Company.create_if_new(c)
                total_added += 1
            except IntegrityError:
                pass
    log.info(f"Scrape complete: {total_added} new companies")
```

Runs daily at 09:00 IST via APScheduler. Also exposed as `POST /admin/scrape` for manual trigger.

### 12.4 Manual CSV upload (admin)
- Endpoint: `POST /admin/companies/import` with CSV file.
- Columns: `name,domain,funding_stage,industry,description`.
- Validation: domain syntactic check.
- Bulk insert with `INSERT OR IGNORE`.

---

## 13. Contact / Email Discovery

### 13.1 Module: `services/contact_finder.py`

Public API:

```python
def find_email(company: Company, full_name: str) -> EmailGuess | None:
    """
    Returns best guess + confidence, or None if nothing plausible.
    """
```

### 13.2 Permutation generation
Given full_name=`Rahul Sharma` and domain=`acme.com`:

```
rahul@acme.com           # 90 confidence
rahul.sharma@acme.com    # 80
r.sharma@acme.com        # 60
rsharma@acme.com         # 55
sharma@acme.com          # 40
rahul_sharma@acme.com    # 30
```

(Confidence is heuristic, used only for sorting display options.)

### 13.3 Verification ladder
For each candidate (in confidence order):
1. **Syntax check** (`email-validator`).
2. **MX lookup** on domain. If no MX → reject all candidates for this domain → return None.
3. **(Optional, rate-limited)** SMTP probe: connect to MX, `RCPT TO` check, log result. Many servers refuse / accept-all → don't trust positively, only trust hard 5xx as negative signal.
4. Return the first non-rejected candidate with its confidence.

### 13.4 Hunter.io stub (Phase 2 wire-up)

```python
def find_email_via_hunter(domain: str, full_name: str) -> EmailGuess | None:
    if not settings.HUNTER_API_KEY:
        return None
    # ... Hunter API call ...
```

When implemented, `find_email` tries Hunter first, falls back to permutation.

### 13.5 UX surface
- `GET /contacts/find?company_id=X&name=...` → JSON `{email, confidence, source}` or `{error}`.
- User confirms → `POST /contacts` saves it (status: pending verification).
- Email shown with confidence badge in UI.

---

## 14. API Specification (JSON over HTTPS)

> Backend exposes ONLY a JSON API. All non-auth routes require a valid session cookie. All endpoints under `/api/v1/`. Auth bootstrap routes under `/auth/`. Health under `/healthz`. Pydantic models define request and response shapes (one source of truth — exported as OpenAPI at `/docs` in dev).

**Conventions**
- Auth: HTTP-only cookie `session=<opaque token>` (set by `/auth/google/callback`). Frontend always sends `credentials: 'include'`.
- Errors: `{ "error": { "code": "snake_case_code", "message": "...", "details": {...} } }`, HTTP status mapped (400/401/403/404/409/422/429/500).
- Pagination: `?limit=50&cursor=<opaque>`; response `{ items: [...], next_cursor: "..." | null }`.
- All timestamps: ISO 8601 UTC (`2026-05-03T10:30:00Z`).

### 14.1 Auth (browser-redirect endpoints, NOT JSON)

| Method | Path | Purpose |
|---|---|---|
| GET | `/auth/login` | Builds Google OAuth URL, sets `oauth_state` cookie, 302 → Google |
| GET | `/auth/google/callback` | Validates state, exchanges code for tokens, creates session, 302 → `${FRONTEND_ORIGIN}/dashboard` (or `/onboarding` if first time / no signature) |
| POST | `/api/v1/auth/logout` | Deletes session row + clears cookie. JSON `{ok:true}` |
| POST | `/api/v1/auth/disconnect` | Revokes Google token, clears stored tokens, deletes session. JSON `{ok:true}` |
| GET | `/api/v1/auth/me` | Current user profile + onboarding status. `{id,email,full_name,is_admin,onboarded,daily_limit,sent_today}` |

### 14.2 Onboarding

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/onboarding` | Body `{sender_signature_name, sender_signature_city}` |

### 14.3 Dashboard

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/dashboard/summary` | `{sent_today, sent_total, replies, bounces, queued, unsubscribed}` |
| GET | `/api/v1/dashboard/activity` | Recent email_logs entries (cursor paginated) |

### 14.4 Companies

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/companies` | Filters: `?stage=&industry=&q=&limit=&cursor=` |
| GET | `/api/v1/companies/{id}` | Detail w/ related contacts |

### 14.5 Contacts

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/contacts` | User-relevant list w/ status |
| POST | `/api/v1/contacts` | Body `{company_id, name, role, email}` |
| GET | `/api/v1/contacts/find` | Query `?company_id=&name=` → `{email, confidence, source}` |
| POST | `/api/v1/contacts/{id}/mark-invalid` | Manual mark-invalid |

### 14.6 Templates

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/templates` | List user's templates |
| POST | `/api/v1/templates` | Create |
| GET | `/api/v1/templates/{id}` | Get one |
| PATCH | `/api/v1/templates/{id}` | Update |
| DELETE | `/api/v1/templates/{id}` | Delete |
| POST | `/api/v1/templates/{id}/preview` | Body `{contact_id?}` → `{subject, body_rendered}` |

### 14.7 Campaigns

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/campaigns` | List w/ counts |
| POST | `/api/v1/campaigns` | Create draft. Body `{name, template_id, followup_template_id?, contact_ids:[]}` |
| GET | `/api/v1/campaigns/{id}` | Detail w/ per-contact rows |
| POST | `/api/v1/campaigns/{id}/launch` | Run moat filter, enqueue. Returns `{queued, skipped, reasons:[{contact_id,reason}]}` |
| POST | `/api/v1/campaigns/{id}/cancel` | Cancel pending sends |

### 14.8 Admin (`users.is_admin=1`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/admin/stats` | Global stats |
| GET | `/api/v1/admin/users` | All users |
| POST | `/api/v1/admin/users/{id}/suspend` | Toggle suspension |
| GET | `/api/v1/admin/companies` | All companies |
| POST | `/api/v1/admin/companies/import` | `multipart/form-data` CSV |
| POST | `/api/v1/admin/scrape` | Trigger scraper now |
| GET | `/api/v1/admin/queue` | Inspect send_queue |
| GET | `/api/v1/admin/logs` | Tail email_logs |

### 14.9 Health

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | `{"status":"ok"}` for UptimeRobot |
| GET | `/readyz` | `{"db":"ok","scheduler":"running"}` |

---

## 15. Frontend (separate repo)

The UI lives in a Next.js (App Router) repo deployed to Vercel. This backend repo does NOT render HTML.

**Contract between them**
- All API access through `NEXT_PUBLIC_API_BASE_URL=https://api.<domain>`.
- All `fetch()` calls use `credentials: 'include'` so the session cookie is sent cross-origin.
- Auth bootstrap is the only non-JSON path: when the user clicks "Connect Gmail", the frontend does `window.location.href = "${API_BASE}/auth/login"`. After Google + our callback, we 302 back to `${FRONTEND_ORIGIN}/dashboard` (or `/onboarding`). No SPA-style code exchange — the browser handles the full redirect chain.
- OpenAPI spec is published at `/openapi.json`; frontend can codegen its client from it.

**Pages the frontend will need (informational only — not built here)**
1. Landing — pitch + "Connect Gmail" button.
2. Onboarding — signature form.
3. Dashboard — stat cards + activity feed.
4. Companies — table with filters.
5. Contacts — saved contacts with status pills.
6. Templates — list + editor with live preview.
7. New campaign — wizard.
8. Campaign detail — per-contact status table.
9. Admin — gated tables.

### 15.2 Starter templates (seeded for new users)

1. **"Job application"**

   ```
   Subject: SDE intern application — {{company}}

   Hi {{first_name}},

   Saw {{company}} just raised seed — congrats. I'm Aarav, a final-year CS
   student at IIIT Delhi. I've shipped {two side projects} and would love
   to contribute to {{company}} as an SDE intern this summer.

   Resume: <link>. Portfolio: <link>.

   Worth a 15-min chat next week?
   ```

2. **"Founder intro"**

   ```
   Subject: Quick idea for {{company}}

   Hey {{first_name}},

   Congrats on the raise. I'm building <X> for <Y> and noticed {{company}}
   would be a natural fit because <one specific reason>. 15 min next week
   to compare notes?
   ```

3. **"Customer discovery"**

   ```
   Subject: 15 min — {{company}} feedback?

   Hi {{first_name}},

   Building <X>; you're exactly the kind of {{role}} we want feedback from.
   Not selling — just learning. Got 15 min next week?
   ```

Plus matching follow-up templates (`is_followup=1`).

---

## 16. Authentication & Sessions

- Login = "Connect Gmail" via Google OAuth (our own client). No username/password.
- Sessions are **server-side opaque tokens** persisted in the `sessions` table (§7.10) — not signed cookies. Reasons: trivial revocation, cross-origin compatibility, no secret-rotation crisis.

### 16.1 `/auth/login` (browser GET)
1. Generate random `state` (32 bytes urlsafe).
2. Set short-lived cookie `oauth_state=<state>` (`HttpOnly; Secure; SameSite=Lax; Max-Age=600`).
3. Build Google authorization URL via `google_auth_oauthlib.flow.Flow` with:
   - `access_type='offline'`
   - `prompt='consent'` (forces refresh_token even on re-auth)
   - `include_granted_scopes='true'`
4. 302 → Google.

### 16.2 `/auth/google/callback` (browser GET)
1. Read `oauth_state` cookie; compare to `request.query_params['state']`. If mismatch → 400.
2. Exchange `code` → tokens via `flow.fetch_token(code=...)`.
3. Decode `id_token` → `sub`, `email`, `name`, `picture`.
4. Verify granted scopes include `gmail.send` AND `gmail.readonly`. If not → 302 → `${FRONTEND_ORIGIN}/connect?error=missing_scopes`.
5. Upsert `users` row keyed by `google_sub`.
6. Fernet-encrypt `refresh_token` and `access_token` and persist along with `expiry`, `scopes`, `google_connected_at`.
7. Create row in `sessions` (random 32-byte token, `expires_at = now() + 30d`).
8. Set cookie:
   ```
   session=<token>; HttpOnly; Secure; SameSite=None;
   Domain=.<root-domain>; Path=/; Max-Age=2592000
   ```
9. Decide redirect target:
   - If `users.sender_signature_name` is null → `${FRONTEND_ORIGIN}/onboarding`.
   - Else → `${FRONTEND_ORIGIN}/dashboard`.

### 16.3 Cookie strategy (cross-origin)
- Frontend on `app.<domain>` (Vercel custom domain) + backend on `api.<domain>` → set cookie with `Domain=.<domain>` so both sub-domains receive it. `SameSite=None; Secure` is mandatory for cross-site.
- During local dev: frontend on `http://localhost:3000`, backend on `http://localhost:8000` → cookie can't be `Secure` over plain HTTP. Override via `COOKIE_SECURE=false` in dev `.env` AND use `SameSite=Lax`. Browsers will send the cookie because both are localhost (same-site by browser semantics on default ports). For ergonomic dev, run backend on `http://localhost:8000` and frontend on `http://localhost:3000`, set `FRONTEND_ORIGIN=http://localhost:3000`, allow CORS from it.
- If using Vercel preview URLs that change per-deploy, configure `ALLOWED_ORIGINS` as a list (regex or explicit) and gate via a CORS allow-list dependency, not just a single string.

### 16.4 Session validation (every authenticated request)
- FastAPI dependency `get_current_user(request, db)`:
  1. Read `session` cookie.
  2. `SELECT * FROM sessions WHERE id=? AND expires_at > now()`. If miss → 401.
  3. Update `last_used_at = now()`. Sliding window.
  4. Load `users` row, attach to request state.
- Dependency `require_admin` wraps `get_current_user` and additionally checks `is_admin`.

### 16.5 CSRF
- We rely on cookies for auth, so CSRF is a real concern for state-changing endpoints. Two layers:
  1. Cookie is `SameSite=None` (required for cross-site auth) — so we cannot rely on browser's same-site protection.
  2. **Required:** every non-GET request must include header `X-Requested-With: XMLHttpRequest`. Browser does NOT send this on a top-level `<form>` submit from another origin without preflight; combined with our CORS allow-list, this gives effective CSRF protection. Our middleware rejects state-changing requests missing the header with 403.
  3. CORS preflight requests come with the same Origin allow-list — if the request origin is not in `ALLOWED_ORIGINS`, the browser blocks it before it reaches us anyway.

### 16.6 Logout / disconnect
- `POST /api/v1/auth/logout` — `DELETE FROM sessions WHERE id=?`; clear cookie via `Set-Cookie: session=; Max-Age=0`.
- `POST /api/v1/auth/disconnect` — `POST https://oauth2.googleapis.com/revoke?token=<refresh_token>`; null out `google_*` fields; delete sessions for the user; clear cookie.

---

## 17. Background Jobs (APScheduler)

| Job | Schedule | Purpose |
|---|---|---|
| `send_worker` | every 1 min | Pull due `send_queue` rows, send |
| `reply_check` | every 30 min | Detect replies (and unsubscribes) |
| `bounce_check` | every 30 min | Detect bounces |
| `followup_queue` | daily at 10:00 IST | Queue follow-ups for eligible UCMs |
| `daily_scrape` | daily at 09:00 IST | Run scrapers |
| `daily_reset` | daily at 00:05 local | Reset `users.sent_today=0` |
| `lock_cleanup` | daily at 04:00 | Delete expired `global_contact_lock` rows |

All jobs:
- Wrapped in try/except, full traceback logged via structlog.
- Open their own short-lived SQLAlchemy session (via `with SessionLocal() as db:`); do NOT share sessions across jobs.
- Run via `BackgroundScheduler` with a `ThreadPoolExecutor(max_workers=2)` to keep RAM low.
- Started exactly once: only when `RUN_SCHEDULER=1` env var is set. Production uses a dedicated systemd unit (`knock-scheduler.service`) separate from the API process; API systemd unit sets `RUN_SCHEDULER=0`.
- Per-job lockfile under `/tmp/outreach-<job>.lock` to prevent overlap if a previous run is still executing (`fcntl.flock`).
- Acquire a per-job DB advisory lock if running multiple processes (Phase 2 / Postgres).

---

## 18. Configuration

### 18.1 `.env` (gitignored)

```
APP_ENV=development                              # development|production
DATABASE_URL=sqlite:///./db/outreach.db
DB_ECHO=false

# Where the Next.js frontend lives. CSV of allowed origins for CORS.
# In prod: https://app.example.com
# In dev:  http://localhost:3000
FRONTEND_ORIGIN=http://localhost:3000
ALLOWED_ORIGINS=http://localhost:3000

# Cookie settings
COOKIE_DOMAIN=                                   # blank in dev; ".example.com" in prod
COOKIE_SECURE=false                              # true in prod
COOKIE_SAMESITE=lax                              # "lax" in dev, "none" in prod (cross-site)

# Google OAuth (from Google Cloud Console → Credentials)
GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxxx
GOOGLE_REDIRECT_URI=http://localhost:8000/auth/google/callback

# Token-at-rest encryption (Fernet key, base64, 32 bytes). Generate once:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
TOKEN_ENCRYPTION_KEY=<base64 fernet key>

# Optional integrations
HUNTER_API_KEY=                 # leave blank for MVP

# Behavior
TIMEZONE=Asia/Kolkata
SEND_HOURS_START=9
SEND_HOURS_END=19
DEFAULT_DAILY_LIMIT=20
HARD_DAILY_CEILING=30
GLOBAL_LOCK_DAYS=2
FOLLOWUP_DELAY_DAYS=4
MAX_FOLLOWUPS=2
SESSION_TTL_DAYS=30

# Operations
LOG_LEVEL=INFO
RUN_SCHEDULER=0                                  # 1 in scheduler systemd unit; 0 in API unit
ADMIN_EMAILS=mridul@example.com                  # comma-sep; auto-promoted on first login
```

### 18.2 `config.py`
- Pydantic `BaseSettings` (from `pydantic-settings`) loads `.env` automatically.
- Exports a singleton `settings` used everywhere.
- All values typed; type errors fail at startup, not runtime.

---

## 19. Project Structure

```
outreach-backend/
├── pyproject.toml               # deps + tool config (ruff, pytest)
├── .env.example                 # committed
├── .gitignore
├── README.md
├── alembic.ini
│
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app factory + uvicorn entry
│   ├── config.py                # pydantic-settings Settings
│   ├── logging.py               # structlog setup
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py              # SQLAlchemy Base + engine + SessionLocal
│   │   ├── session.py           # get_db() dependency
│   │   └── seed.py              # seeds starter templates on first init
│   │
│   ├── models/                  # SQLAlchemy ORM (one file per table family)
│   │   ├── __init__.py
│   │   ├── user.py
│   │   ├── session.py           # `sessions` table
│   │   ├── company.py
│   │   ├── contact.py
│   │   ├── template.py
│   │   ├── campaign.py
│   │   ├── send_queue.py
│   │   ├── user_contact_map.py
│   │   ├── global_lock.py
│   │   └── email_log.py
│   │
│   ├── schemas/                 # Pydantic request/response models
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── user.py
│   │   ├── company.py
│   │   ├── contact.py
│   │   ├── template.py
│   │   ├── campaign.py
│   │   └── common.py            # Pagination, ErrorEnvelope
│   │
│   ├── core/
│   │   ├── deps.py              # get_db, get_current_user, require_admin
│   │   ├── errors.py            # ApiError + handler
│   │   ├── responses.py         # success/error envelope helpers
│   │   ├── csrf.py              # X-Requested-With check middleware
│   │   ├── crypto.py            # Fernet encrypt/decrypt
│   │   └── time.py              # tz-aware now(), utc helpers
│   │
│   ├── services/
│   │   ├── google_client.py     # OAuth + Gmail API wrapper (§8)
│   │   ├── email_sender.py      # render + send_one
│   │   ├── reply_detector.py
│   │   ├── bounce_detector.py
│   │   ├── contact_finder.py
│   │   ├── unsubscribe.py
│   │   ├── moat.py              # global_lock + ucm helpers
│   │   └── template_render.py   # sandboxed Jinja2 env for email body
│   │
│   ├── scrapers/
│   │   ├── base.py              # CompanyCandidate dataclass
│   │   ├── parser_utils.py
│   │   ├── techcrunch.py
│   │   ├── yourstory.py
│   │   ├── inc42.py
│   │   ├── google_news.py
│   │   └── orchestrator.py
│   │
│   ├── jobs/
│   │   ├── scheduler.py         # APScheduler bootstrap (gated by RUN_SCHEDULER)
│   │   ├── send_worker.py
│   │   ├── reply_check.py
│   │   ├── bounce_check.py
│   │   ├── followup_queue.py
│   │   ├── daily_scrape.py
│   │   ├── daily_reset.py
│   │   └── lock_cleanup.py
│   │
│   └── routers/                 # FastAPI APIRouter modules
│       ├── __init__.py          # central registration
│       ├── auth.py              # /auth/* (browser redirects) + /api/v1/auth/*
│       ├── onboarding.py
│       ├── dashboard.py
│       ├── companies.py
│       ├── contacts.py
│       ├── templates.py
│       ├── campaigns.py
│       ├── admin.py
│       └── health.py
│
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│
├── tests/
│   ├── conftest.py
│   ├── test_auth_flow.py
│   ├── test_moat.py             # CRITICAL: cross-user lock invariant
│   ├── test_send_pipeline.py
│   ├── test_followup.py
│   ├── test_reply_detection.py
│   ├── test_bounce_detection.py
│   ├── test_contact_finder.py
│   └── test_scrapers.py
│
└── deploy/
    ├── systemd/knock-api.service
    ├── systemd/knock-scheduler.service
    └── Caddyfile
```

---

## 20. Setup Instructions

### 20.1 Local dev

```bash
git clone <repo> && cd outreach-backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# fill in GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, TOKEN_ENCRYPTION_KEY
alembic upgrade head            # creates SQLite schema
python -m app.db.seed           # seeds starter templates
RUN_SCHEDULER=1 uvicorn app.main:app --reload --port 8000
# OpenAPI docs at http://localhost:8000/docs
```

### 20.2 `pyproject.toml` (deps)

```toml
[project]
name = "outreach-backend"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "pydantic>=2.7",
  "pydantic-settings>=2.4",
  "sqlalchemy>=2.0",
  "alembic>=1.13",
  "apscheduler>=3.10",
  "google-auth>=2.30",
  "google-auth-oauthlib>=1.2",
  "google-api-python-client>=2.140",
  "cryptography>=42.0",
  "httpx>=0.27",
  "feedparser>=6.0",
  "selectolax>=0.3.21",
  "dnspython>=2.6",
  "email-validator>=2.2",
  "jinja2>=3.1",                # email body rendering only
  "structlog>=24.1",
  "python-multipart>=0.0.9",    # CSV upload
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "ruff>=0.6", "mypy>=1.11", "httpx"]
```

### 20.3 Production (Hetzner CX11, Ubuntu 22.04, 1 GB RAM)

```bash
# As root
apt update && apt install -y python3.11 python3.11-venv git caddy
useradd -m -s /bin/bash outreach
su - outreach
git clone <repo> && cd outreach-backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env  # edit with prod values (FRONTEND_ORIGIN, COOKIE_DOMAIN, etc.)
alembic upgrade head
python -m app.db.seed
exit  # back to root
cp deploy/systemd/knock-api.service       /etc/systemd/system/
cp deploy/systemd/knock-scheduler.service /etc/systemd/system/
systemctl enable --now knock-api knock-scheduler
cp deploy/Caddyfile /etc/caddy/Caddyfile
systemctl reload caddy
```

### 20.4 systemd: API unit (`knock-api.service`)

```ini
[Unit]
Description=Knock Backend API
After=network.target

[Service]
User=outreach
WorkingDirectory=/home/outreach/outreach-backend
EnvironmentFile=/home/outreach/outreach-backend/.env
Environment=RUN_SCHEDULER=0
ExecStart=/home/outreach/outreach-backend/.venv/bin/uvicorn \
          app.main:app --host 127.0.0.1 --port 8000 \
          --workers 1 --loop uvloop --http httptools
Restart=on-failure
MemoryMax=512M

[Install]
WantedBy=multi-user.target
```

### 20.5 systemd: Scheduler unit (`knock-scheduler.service`)

```ini
[Unit]
Description=Knock Background Scheduler
After=network.target knock-api.service

[Service]
User=outreach
WorkingDirectory=/home/outreach/outreach-backend
EnvironmentFile=/home/outreach/outreach-backend/.env
Environment=RUN_SCHEDULER=1
ExecStart=/home/outreach/outreach-backend/.venv/bin/python -m app.jobs.scheduler
Restart=on-failure
MemoryMax=256M

[Install]
WantedBy=multi-user.target
```

> Both processes share the same SQLite file. Enable WAL mode in `app/db/base.py` (`PRAGMA journal_mode=WAL`) so reads don't block writes. Total RAM budget: ~768M of the 1GB; headroom for OS + Caddy.

### 20.6 Caddyfile

```
api.your-domain.com {
    reverse_proxy 127.0.0.1:8000
    encode gzip zstd
}
```

---

## 21. Testing Strategy

### 21.1 Critical tests (must exist before launch)
1. **Moat invariant** — concurrent campaign launches by 2 users cannot both queue contact X.
2. **Daily limit** — sending pauses at `daily_limit` and resumes next day.
3. **Reply cancels follow-up** — a reply mid-flight kills any queued follow-up.
4. **Bounce marks invalid** — and prevents future sends to same email.
5. **Unsubscribe footer present** in 100% of `send_email` payloads.
6. **Token revoked** — user's Gmail disconnect leads to graceful suspension, not crashes.
7. **Idempotent send** — sending twice with same queue_row.id is a no-op.

### 21.2 Manual QA checklist before beta
- [ ] OAuth round-trip from incognito.
- [ ] First send arrives w/ correct From.
- [ ] Reply on phone → app detects within 30 min.
- [ ] Hard bounce → contact marked invalid → no follow-up.
- [ ] Two test users on same contact → second user blocked.
- [ ] Unsubscribe reply → no follow-up sent.
- [ ] Daily limit hit → new sends pushed to tomorrow.
- [ ] Scraper run → ≥10 new companies added.
- [ ] CSV upload of 100 companies → no duplicates.
- [ ] Admin can suspend a user → their queue freezes.

---

## 22. Compliance & Legal

### 22.1 Required (non-negotiable)
- **Unsubscribe mechanism** in every email (text-based "reply unsubscribe").
- **Sender identification** (real name + city) in every email.
- **Privacy policy** page accessible from landing.
- **Terms of service** page covering: user is responsible for outbound content, prohibition on harassment / spam / illegal content, account termination triggers.
- **Data deletion** — admin action: delete a user's data on request (GDPR/DPDP friendly).

### 22.2 OAuth scope justification (Google verification submission)
Document on the privacy policy AND in the Google verification form why we need each restricted scope:

- **`https://www.googleapis.com/auth/gmail.send`** — Required to send outreach emails composed by the user from their own Gmail account. The user explicitly authors templates and selects recipients in our app; we send only what they instruct, and only to the contacts they have approved. Without this scope the core product is impossible.
- **`https://www.googleapis.com/auth/gmail.readonly`** — Required to detect replies and bounces *only* on threads our app initiated. We use the user-specific `threadId` we stored at send time to call `users.threads.get`. We do not enumerate or read the user's broader inbox. Bounce detection additionally uses a narrow query (`from:mailer-daemon newer_than:1d`) to find delivery failures for sends we initiated.

**Data handling commitments (also documented on privacy policy):**
- We do not retain the body content of replies — only the fact that a reply was detected and the timestamp.
- Tokens are encrypted at rest (Fernet); only the user's own session can trigger reads of their inbox.
- Users can disconnect anytime via `/auth/disconnect`, which calls Google's revoke endpoint and deletes our stored tokens.
- We do not transfer Gmail data to third parties or use it for ads / model training.

These commitments must be reflected verbatim on the public privacy policy URL submitted with the verification form.

---

## 23. 4-Week Phased Roadmap

### Pre-flight (Day 0)
- [ ] Create Google Cloud project. Enable Gmail API.
- [ ] Configure OAuth consent screen (External, Testing). Add yourself as test user.
- [ ] Create OAuth Client ID (Web application). Save `client_id` + `client_secret`.
- [ ] Generate Fernet `TOKEN_ENCRYPTION_KEY`.
- [ ] Buy domain. Spin up Hetzner CX11. Point DNS.
- [ ] Add prod redirect URI to OAuth client: `https://<domain>/auth/google/callback`.
- [ ] Decide sender signature requirement (locked: required before first send).

### Week 1 — Foundation
- **Day 1–2** Project skeleton (FastAPI + Pydantic + SQLAlchemy + Alembic), SQLite schema via first migration, seed script. FastAPI hello-world (`/healthz`) deployed on VM w/ Caddy TLS.
- **Day 3–4** `services/google_client.py` (OAuth flow + Gmail API helpers). `services/crypto.py` (Fernet wrap). `/auth/login` + `/auth/google/callback`. User row created on first connect with refresh_token encrypted at rest.
- **Day 5–6** `POST /test-send` hardcoded → real email lands in target inbox. Verify From header.
- **Day 7** Onboarding form (signature). Dashboard with "connected" state.
- **Acceptance:** logged-in user can send 1 hand-crafted email through UI button → arrives, has unsubscribe footer.

### Week 2 — Data engine
- **Day 8–9** RSS scrapers (4 sources) + parser utils.
- **Day 10** APScheduler bootstrap, daily scrape job, manual trigger endpoint.
- **Day 11–12** Contact finder (permutation + MX). `/api/contacts/find`.
- **Day 13–14** Companies list/detail UI. Contacts list. "Save contact" flow.
- **Acceptance:** 1 scrape run yields ≥20 companies; user can find + save a contact in <60 sec.

### Week 3 — Sending pipeline
- **Day 15–16** Templates CRUD + preview.
- **Day 17–18** Campaign wizard. Pre-send filter (the moat). Skip reasons surfaced.
- **Day 19–20** `send_queue` + `send_worker`. Daily limit, jitter, success path.
- **Day 21** Error paths: retries, FAILED, suspension on auth error.
- **Acceptance:** campaign of 10 contacts → 10 emails delivered over 1–2 hours, all with footer, all with `user_contact_map` rows + global locks.

### Week 4 — Tracking, follow-ups, polish
- **Day 22–23** Reply detection job. UCM transitions to REPLIED. Follow-up cancellation.
- **Day 24** Bounce detection. Contact marked invalid.
- **Day 25–26** Follow-up queue job. In-thread reply.
- **Day 27** Dashboard stats. Campaign detail page w/ per-contact status.
- **Day 28** Admin panel + final QA pass.
- **Acceptance:** end-to-end test on 2 inboxes (one as sender, one as recipient): send → reply → no follow-up; send → no reply → follow-up after 4 days (or fast-forward via clock manipulation).

### Post-launch (Week 5+)
- Onboard 5 friends (must be added as Test users in Google OAuth consent screen if still in Testing mode).
- Add Hunter.io.
- Publish OAuth consent screen to Production once stable (still capped at 100 users until verification).
- At ~80 users: kick off Google verification (4–8 weeks) + third-party security assessment.
- Postgres migration trigger: 50 concurrent users OR write contention visible.

---

## 24. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Hit 100-user OAuth cap | High eventually | Growth blocked | Track unique connected users; trigger verification submission at ~80 |
| Google verification rejected | Medium | Delayed scaling | Submit early, address feedback iteratively; have SMTP-app-password fallback flow designed |
| Refresh token revoked by user | Medium | Per-user breakage | Detect 401, mark suspended, surface "reconnect" CTA; no data loss |
| Token-at-rest leak | Low | Major | Fernet encryption + restrictive file perms on `.env`; rotate `TOKEN_ENCRYPTION_KEY` if compromised (forces all users to reconnect) |
| Google API outage | Low | Sends pause | Queue absorbs the pause; alert admin via UptimeRobot |
| User's Gmail flagged for spam | Medium | One user breaks | Daily cap hardcoded ≤30; require sender signature; force unsubscribe footer |
| Bounce rate spikes (bad guesses) | High initially | Reputation damage | Conservative confidence threshold (≥60); cap initial sends per user to 10/day until first 30 confirmed delivered |
| Scraper site changes RSS schema | Medium | Pipeline degrades | Per-source try/except, alert on 2 consecutive 0-result runs |
| SQLite write contention | Low at MVP | Slowdowns | Migration plan to Postgres documented |
| Two users emailing same contact (moat bug) | High if untested | Brand-killer | Dedicated test_moat.py with concurrency test |
| Legal: missed unsubscribe | Low | DPDP exposure | Footer rendered server-side, not user-controllable; tested |

---

## 25. Cost Model

### 25.1 MVP (0–20 users, <500 emails/day)

| Item | ₹/mo |
|---|---|
| Hetzner CX11 VPS | 350 |
| Domain | 70 |
| Google OAuth + Gmail API | 0 |
| **Total** | **~420** |

### 25.2 Growth (100 users, ~2,000 emails/day, still unverified or just verified)

| Item | ₹/mo |
|---|---|
| Hetzner CX21 (upgrade) | 700 |
| Domain | 70 |
| Gmail API | 0 |
| Hunter.io | 4,000 |
| **Total** | **~4,800** |

### 25.3 One-time / annual costs to scale past 100 users
- **Google OAuth verification (security assessment):** ₹80,000–₹2,00,000 one-time, repeated annually. Performed by a Google-approved third-party auditor (Bishop Fox, Leviathan, KPMG, etc.). Required to lift the 100-user cap.
- **Brand verification:** free, ~2–4 weeks of back-and-forth with Google (privacy policy review, demo video, domain verification).

### 25.4 Scale-out trigger
- ~80 connected users → submit for verification.
- Verification cost amortized > revenue → keep going.
- If audit cost not feasible → cap product at 100 users OR pivot to user-supplied SMTP app passwords (no scope verification needed).

---

## 26. Out-of-Scope / Future Roadmap (post-MVP)

- Team/org accounts (multi-seat, shared contacts within org).
- AI personalization (LLM rewrites template per contact using LinkedIn snippet).
- A/B test of subject lines.
- Outlook / Microsoft 365 support (separate OAuth client + Microsoft Graph API).
- Custom domain warmup (for users without prior Gmail history).
- Calendar embed (scheduling link in template).
- Stripe billing (free tier 50 sends/mo, paid tier higher cap).
- LinkedIn DM channel (separate compliance review).
- iOS / Android app.
- Webhook from Gmail Push API (Pub/Sub `watch` + `historyId`) instead of polling (saves Gmail API quota and gives near-real-time reply detection).

---

## 27. Glossary

- **UCM** — `user_contact_map`, the per-user lifecycle row.
- **Global lock** — entry in `global_contact_lock`; prevents cross-user duplicate outreach for 30 days.
- **Refresh token** — Google long-lived credential; we encrypt at rest with Fernet and use it to mint short-lived access tokens.
- **Restricted scope** — Google's classification for high-sensitivity scopes (`gmail.send`, `gmail.readonly`); requires verification + security assessment to lift the 100-user cap.
- **Initial vs follow-up** — distinguished by `send_queue.kind` and template's `is_followup`.
- **Moat filter** — pre-send check: `not user_already_contacted AND not globally_locked_by_other`.

---

## 28. Open Questions (resolve before coding)

1. **Sender signature** required? **Decision:** YES, required before first send. Block UI otherwise.
2. **Cooldown duration?** **Decision:** 30 days global, permanent per-user (UCM persists).
3. **Multiple follow-up templates per campaign?** **Decision:** No, MVP supports one follow-up template; max 2 follow-ups, both use same template.
4. **Allow user-supplied SMTP** (fallback for users who can't grant restricted scopes, or to bypass the 100-user cap)? **Decision:** Phase 2; not MVP.
5. **Multi-language email templates?** **Decision:** Phase 2; English only.
6. **What happens after a contact unsubscribes via reply?** **Decision:** That user is permanently blocked from re-emailing (UCM stays). Other users can still email after lock expires (we can't tell them about the unsubscribe — it was on user A's email, not a global signal).
7. **Domain alias for sending (e.g., `aarav@aarav.com` if user has it)?** **Decision:** No, use whatever Gmail returns as the user's primary address. Phase 2 can support Gmail "Send As" aliases.

---

## 29. Acceptance Criteria for "MVP done"

A new user can:
1. Land on the homepage.
2. Click "Connect Gmail" → complete OAuth → land on `/onboarding`.
3. Submit signature → land on `/dashboard`.
4. Browse companies (≥50 present from scraper) → save 5 contacts (with email guesses).
5. Create a template using placeholders → preview correctly.
6. Launch a campaign with those 5 contacts → see "Queued: 5".
7. Within ≤1 hour: 5 emails arrive at the targets, all with unsubscribe footer.
8. Reply from one target → within 30 min, dashboard shows REPLIED for that contact.
9. Wait 4 days (or admin-trigger fast-forward) → follow-ups go out for the 4 unreplied, NOT the replied one.
10. Admin can view all this activity from `/admin`.

When all 10 work end-to-end without manual intervention, **MVP is shipped.**

---

## 30. Build Order (suggested, for the implementing LLM)

1. `db/schema.sql` and `db/init_db.py` — verify with `sqlite3 db/outreach.db .schema`.
2. `models/*.py` — SQLAlchemy mirroring DDL exactly.
3. `config.py` + `.env.example`.
4. `app.py` factory with blueprints stubbed.
5. `services/crypto.py` (Fernet wrapper) + `services/google_client.py` (OAuth flow + Gmail API helpers) — isolate the external dep.
6. `routes/auth.py` (`/auth/login`, `/auth/google/callback`, `/auth/logout`, `/auth/disconnect`) + `templates/landing.html` + `onboarding.html` — first end-to-end slice.
7. **At this point: deploy and verify OAuth on prod domain.** Prod-first reduces "works on my localhost" surprises with OAuth callback URLs.
8. `services/email_sender.py` + `routes/api.py /test-send` — second end-to-end slice (a real email!).
9. Scrapers + `daily_scrape` job.
10. `services/contact_finder.py` + UI.
11. Templates CRUD.
12. Campaigns CRUD + launch + `send_worker`.
13. Reply / bounce / followup jobs.
14. Dashboard + admin polish.
15. Tests for moat invariants + acceptance walkthrough.

End of PRD.
