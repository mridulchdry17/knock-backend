# Knock — Frontend Build Spec

> Hand this file to the frontend repo / agent. It contains everything needed to build the UI without reading the backend code or the full PRD. For the product story (why we're building this), skim [`prd.md` §1–3](prd.md). For the deep API surface read [`prd.md` §14](prd.md). This file is the **contract**: stable types, URLs, auth model, and screen specs.

---

## 1. The product in one paragraph

**Knock** is a multi-user cold-outreach tool. A user connects their personal Gmail (Google OAuth), picks contacts from a daily-refreshed pool of startups, and the backend sends personalized emails from their inbox with auto follow-ups. The differentiator: a **cross-user 2-day lock** ensures no two Knock users ever email the same person at the same time.

The frontend's job is everything user-facing: landing, OAuth bootstrap, dashboard, contact browsing, template editing, campaign creation, and per-contact status tracking.

---

## 2. Recommended stack

| Layer | Pick |
|---|---|
| Framework | **Next.js 14+ (App Router)** |
| Language | TypeScript (strict) |
| Styling | Tailwind CSS |
| UI primitives | shadcn/ui |
| Server state | **TanStack Query v5** |
| Forms | react-hook-form + zod |
| Icons | lucide-react |
| Hosting | Vercel |
| Auth model | Backend-issued HTTP-only cookie (we do NOT manage tokens client-side) |

> Codegen: backend publishes OpenAPI at `https://api.<env>/openapi.json`. Use `openapi-typescript` to generate types if you prefer auto-sync over the hand-written types in §6.

---

## 3. Backend connection

| | Dev | Production |
|---|---|---|
| Backend base URL | `http://localhost:8000` | `https://api.<your-domain>` |
| Frontend origin | `http://localhost:3000` | `https://<your-domain>` (or `*.vercel.app` for previews) |
| OpenAPI docs | `http://localhost:8000/docs` | (disabled in prod) |

Set in the frontend's `.env.local`:

```
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

In production set `NEXT_PUBLIC_API_BASE_URL=https://api.<your-domain>` on Vercel.

---

## 4. Auth model — read this carefully

### 4.1 How the user logs in

There is **no email/password and no OAuth code-exchange in the frontend**. Auth is a 100% browser-redirect chain handled by the backend:

```
[click "Connect Gmail"]
   └─► window.location.href = `${API_BASE}/auth/login`
         └─► (backend) sets oauth_state cookie, 302 to Google
               └─► user picks Google account, grants Gmail scopes
                     └─► Google 302 → ${API_BASE}/auth/google/callback?code=...&state=...
                           └─► (backend) verifies, persists tokens, issues session,
                               sets `session` cookie, 302 to:
                                 - ${FRONTEND_ORIGIN}/onboarding   (first-time / no signature)
                                 - ${FRONTEND_ORIGIN}/dashboard    (returning user)
```

The frontend's only job in this chain is the very first line (`window.location.href`) and rendering whatever target page the backend redirects to.

### 4.2 Session cookie

After the round-trip, the backend has set:

```
Set-Cookie: session=<opaque-token>;
            HttpOnly; Secure; SameSite=None; Domain=.<root-domain>; Max-Age=2592000
```

- The cookie is **HTTP-only** — JavaScript cannot read it. **This is by design.**
- Frontend never inspects, parses, or stores the cookie. The browser sends it automatically on every request as long as you set `credentials: 'include'`.
- "Am I logged in?" → call `GET /api/v1/auth/me`. If 200 → logged in. If 401 → not logged in. There is no other source of truth.

### 4.3 Every fetch must include credentials

```ts
fetch(`${API_BASE}/api/v1/auth/me`, {
  credentials: 'include',
});
```

If you forget `credentials: 'include'`, the cookie won't be sent and you'll get 401s for no apparent reason. Wrap your fetch in a tiny client and never bypass it (see §5).

### 4.4 CSRF — add `X-Requested-With` to every state-changing request

The backend rejects every non-GET request to `/api/v1/*` that doesn't carry:

```
X-Requested-With: XMLHttpRequest
```

This is a deliberate, lightweight CSRF guard. (Browsers will not attach this header on a cross-site form submit, so the only sources are first-party JS — combined with our CORS allow-list, that's effective protection without a CSRF-token table.)

A request without it returns:

```json
{ "error": { "code": "csrf_blocked", "message": "Missing required header X-Requested-With." } }
```

### 4.5 Logout

```ts
await api.post('/api/v1/auth/logout');           // clears cookie + session row
router.replace('/');                              // back to landing
```

### 4.6 Disconnect (revoke Gmail)

```ts
await api.post('/api/v1/auth/disconnect');        // tells Google to revoke our refresh_token,
                                                  // nukes our stored tokens, logs user out
router.replace('/');
```

---

## 5. Recommended HTTP client (≤ 30 lines)

`src/lib/api.ts`:

```ts
const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL!;

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: unknown,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  if (init.body) headers.set('Content-Type', 'application/json');
  if (method !== 'GET' && method !== 'HEAD') {
    headers.set('X-Requested-With', 'XMLHttpRequest');
  }

  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
    credentials: 'include',
  });

  if (res.status === 204) return undefined as T;

  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = data?.error ?? {};
    throw new ApiError(res.status, e.code ?? 'unknown', e.message ?? res.statusText, e.details);
  }
  return data as T;
}

export const api = {
  get:    <T>(p: string)         => request<T>(p),
  post:   <T>(p: string, b?: any) => request<T>(p, { method: 'POST',   body: b ? JSON.stringify(b) : undefined }),
  patch:  <T>(p: string, b?: any) => request<T>(p, { method: 'PATCH',  body: b ? JSON.stringify(b) : undefined }),
  del:    <T>(p: string)         => request<T>(p, { method: 'DELETE' }),
};
```

Use this everywhere — never raw `fetch`.

---

## 6. Type definitions (TypeScript)

Match the Pydantic schemas the backend exposes. Keep this file in sync (or codegen).

`src/lib/types.ts`:

```ts
// ── Common ────────────────────────────────────────────────────────────
export interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
    details?: unknown;
  };
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface Ok {
  ok: true;
}

// ── Auth ──────────────────────────────────────────────────────────────
export interface Me {
  id: number;
  email: string;
  full_name: string | null;
  is_admin: boolean;
  onboarded: boolean;             // true once sender_signature_name is set
  has_gmail_connected: boolean;
  daily_limit: number;
  sent_today: number;
}

// ── Onboarding (Phase 2 — planned) ────────────────────────────────────
export interface OnboardingInput {
  sender_signature_name: string;  // required, used in unsubscribe footer
  sender_signature_city: string;  // required, used in unsubscribe footer
}

// ── Companies (Phase 4 — planned) ─────────────────────────────────────
export interface Company {
  id: number;
  domain: string;
  name: string;
  source: string;                 // 'techcrunch'|'yourstory'|'inc42'|'gnews'|'manual'
  funding_stage: string | null;   // 'pre_seed'|'seed'|'series_a'|...
  industry: string | null;
  article_url: string | null;
  description: string | null;
  created_at: string;             // ISO 8601 UTC
}

// ── Contacts (Phase 4 — planned) ──────────────────────────────────────
export interface Contact {
  id: number;
  company_id: number;
  name: string | null;
  role: string | null;
  email: string | null;
  email_source: 'guess' | 'hunter' | 'manual' | null;
  email_confidence: number | null;  // 0–100
  email_verified: boolean;
  is_invalid: boolean;
}

export interface ContactGuess {
  email: string;
  confidence: number;
  source: 'guess' | 'hunter';
}

// ── Templates (Phase 3 — planned) ─────────────────────────────────────
export interface Template {
  id: number;
  name: string;
  subject: string;                // supports {{name}} {{first_name}} {{company}} {{role}}
  body: string;
  is_followup: boolean;
  parent_template_id: number | null;
}

export interface TemplatePreview {
  subject: string;
  body_rendered: string;
}

// ── Campaigns (Phase 5 — planned) ─────────────────────────────────────
export type CampaignStatus = 'DRAFT' | 'RUNNING' | 'COMPLETED' | 'CANCELLED';

export interface Campaign {
  id: number;
  name: string;
  template_id: number;
  followup_template_id: number | null;
  status: CampaignStatus;
  queued_count: number;
  sent_count: number;
  replied_count: number;
  bounced_count: number;
  skipped_count: number;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface LaunchCampaignResult {
  queued: number;
  skipped: number;
  reasons: { contact_id: number; reason: string }[];   // 'already_contacted'|'locked_by_other'|'no_email'|'invalid'
}

// ── Send-status per contact (within a campaign) ───────────────────────
export type ContactStatus =
  | 'QUEUED' | 'SENT' | 'FOLLOWUP_SENT'
  | 'REPLIED' | 'BOUNCED' | 'UNSUBSCRIBED' | 'DEAD';

export interface CampaignContactRow {
  contact_id: number;
  contact_name: string | null;
  company_name: string;
  email: string | null;
  status: ContactStatus;
  sent_at: string | null;
  followup_count: number;
}

// ── Dashboard (Phase 8 — planned) ─────────────────────────────────────
export interface DashboardSummary {
  sent_today: number;
  sent_total: number;
  replies: number;
  bounces: number;
  queued: number;
  unsubscribed: number;
}
```

---

## 7. Endpoint reference

> Status legend matches the PRD. Frontend should code defensively against the **planned** ones — wire the call paths but expect them to land over the next phases.

### 7.1 Auth (live)

| Method | Path | Body | Response | Notes |
|---|---|---|---|---|
| GET | `/auth/login` | — | 302 (browser nav, **not fetch**) | Click handler: `window.location.href = ${API_BASE}/auth/login` |
| GET | `/auth/google/callback` | (server-handled) | 302 to FE `/onboarding` or `/dashboard` | Don't call directly |
| GET | `/api/v1/auth/me` | — | `Me` | 401 ⇒ not logged in |
| POST | `/api/v1/auth/logout` | — | `Ok` | Clears cookie |
| POST | `/api/v1/auth/disconnect` | — | `Ok` | Revokes Google + clears all sessions |

### 7.2 Onboarding (Phase 2 — planned)

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/v1/onboarding` | `OnboardingInput` | `Ok` |

After success, refetch `Me` to confirm `onboarded: true`.

### 7.3 Dashboard (Phase 8 — planned)

| Method | Path | Response |
|---|---|---|
| GET | `/api/v1/dashboard/summary` | `DashboardSummary` |
| GET | `/api/v1/dashboard/activity` | `Page<ActivityRow>` |

### 7.4 Companies (Phase 4 — planned)

| Method | Path | Query | Response |
|---|---|---|---|
| GET | `/api/v1/companies` | `?stage=&industry=&q=&limit=&cursor=` | `Page<Company>` |
| GET | `/api/v1/companies/{id}` | — | `Company & { contacts: Contact[] }` |

### 7.5 Contacts (Phase 4 — planned)

| Method | Path | Body / Query | Response |
|---|---|---|---|
| GET | `/api/v1/contacts` | `?status=&limit=&cursor=` | `Page<Contact>` |
| POST | `/api/v1/contacts` | `{ company_id, name, role, email }` | `Contact` |
| GET | `/api/v1/contacts/find` | `?company_id=&name=` | `ContactGuess` |
| POST | `/api/v1/contacts/{id}/mark-invalid` | — | `Ok` |

### 7.6 Templates (Phase 3 — planned)

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/v1/templates` | — | `Template[]` |
| POST | `/api/v1/templates` | `Pick<Template, 'name'\|'subject'\|'body'\|'is_followup'>` | `Template` |
| GET | `/api/v1/templates/{id}` | — | `Template` |
| PATCH | `/api/v1/templates/{id}` | `Partial<Template>` | `Template` |
| DELETE | `/api/v1/templates/{id}` | — | `Ok` |
| POST | `/api/v1/templates/{id}/preview` | `{ contact_id?: number }` | `TemplatePreview` |

### 7.7 Campaigns (Phase 5 — planned)

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/v1/campaigns` | — | `Campaign[]` |
| POST | `/api/v1/campaigns` | `{ name, template_id, followup_template_id?, contact_ids: number[] }` | `Campaign` |
| GET | `/api/v1/campaigns/{id}` | — | `Campaign & { rows: CampaignContactRow[] }` |
| POST | `/api/v1/campaigns/{id}/launch` | — | `LaunchCampaignResult` |
| POST | `/api/v1/campaigns/{id}/cancel` | — | `Ok` |

---

## 8. Page-by-page spec

### 8.1 `/` — Landing (public)

- Headline + subhead pitching Knock.
- Single CTA: **"Connect Gmail"**.
  - On click: `window.location.href = ${NEXT_PUBLIC_API_BASE_URL}/auth/login`
- If user is already logged in (probe `/api/v1/auth/me` server-side or in a `useEffect`), redirect to `/dashboard`.

### 8.2 `/onboarding` — First-time setup (logged-in only)

- Single form, two required fields:
  - `sender_signature_name` (text)
  - `sender_signature_city` (text)
- These appear in every email's unsubscribe footer (legal requirement).
- Show context: "These will appear at the bottom of every email you send. Use your real name and city — emails without an identifiable sender are blocked."
- Submit → `POST /api/v1/onboarding`. On success: `router.replace('/dashboard')`.
- Redirect away if `me.onboarded === true`.

### 8.3 `/dashboard` — Home for logged-in users

- 4 stat cards (top): Sent today / Replies / Bounces / Queued.
  - Source: `/api/v1/dashboard/summary`.
- "Recent activity" feed (timeline list of last 20 events).
  - Source: `/api/v1/dashboard/activity`.
- Quick links to: Companies, Templates, Campaigns.
- Banner if `me.has_gmail_connected === false` → "Reconnect Gmail" → `/auth/login`.

### 8.4 `/companies` — Browse the pool

- Server-side filterable table (TanStack Query + cursor pagination):
  - Filters: funding stage, industry, free-text.
  - Columns: Name, Domain, Stage, Industry, "View".
- Click row → `/companies/[id]`.

### 8.5 `/companies/[id]`

- Company header (name, domain, stage, source link).
- Contact list (existing rows). Below it: an inline "Find contact" form
  - Inputs: full name (text). Submit → `GET /api/v1/contacts/find?company_id=&name=`.
  - Shows `{ email, confidence, source }` with a colored confidence pill (red < 60, amber 60–79, green ≥ 80).
  - "Save contact" → `POST /api/v1/contacts`.

### 8.6 `/contacts` — User's saved contacts

- Filter: status (NEW / SENT / REPLIED / BOUNCED / DEAD / UNSUBSCRIBED).
- Columns: Name, Company, Role, Email, Status (pill), Last action.
- Bulk-select for adding to a campaign (Phase 5).

### 8.7 `/templates`

- Two-pane layout: list on the left, editor on the right.
- Editor fields: name, subject, body (textarea / code editor with placeholder hints).
- Live preview pane:
  - Pick a sample contact (or use defaults: name="Rahul Sharma", company="Acme", role="Founder").
  - Calls `POST /api/v1/templates/{id}/preview` on a debounce.
- "Mark as follow-up" toggle. Follow-up templates can optionally `parent_template_id` reference an initial template (UI relation only; backend just stores it).

### 8.8 `/campaigns/new` — 3-step wizard

1. **Pick template** (initial + optional follow-up).
2. **Select contacts** (multi-checkbox table from `/api/v1/contacts`, with the same filters as 8.6).
3. **Review & launch** — show counts, then `POST /api/v1/campaigns` (creates DRAFT) → `POST /api/v1/campaigns/{id}/launch`.

After launch, render the result toast/banner with breakdown:
> "Queued 17, skipped 3 (2 already contacted, 1 locked by another user)."

### 8.9 `/campaigns/[id]` — Detail

- Stat strip: queued / sent / replied / bounced / skipped.
- Per-contact rows table (from `Campaign & { rows }`). Status column uses a colored pill.
- "Cancel campaign" button if `status === 'RUNNING'` or `'DRAFT'`.

### 8.10 `/admin/*` (visible only if `me.is_admin === true`)

Tables:
- All users (suspend toggle).
- All companies (CSV import button).
- Send queue (read-only).
- Email log tail.
- "Run scrapers now" button.

---

## 9. Error handling pattern

The backend returns a uniform envelope:

```json
{ "error": { "code": "validation_error", "message": "...", "details": { ... } } }
```

In the frontend:

```tsx
try {
  await api.post('/api/v1/templates', payload);
} catch (e) {
  if (e instanceof ApiError) {
    if (e.status === 401) router.replace('/');
    else if (e.code === 'csrf_blocked') /* shouldn't happen if api client used */;
    else toast.error(e.message);
  }
}
```

Common codes:
- `unauthorized` (401) — session expired → redirect to landing.
- `forbidden` (403) — admin-only or suspended.
- `csrf_blocked` (403) — wrong header. Don't retry; fix the client.
- `validation_error` (422) — `details.errors` is a Pydantic error array.
- `rate_limited` (429) — retry with backoff.

---

## 10. Loading & auth-gate strategy

`<RootLayout>` wraps every authenticated route in an auth probe:

```tsx
'use client';
const { data: me, isLoading, isError } = useQuery({
  queryKey: ['me'],
  queryFn: () => api.get<Me>('/api/v1/auth/me'),
  retry: false,
  staleTime: 60_000,
});

if (isLoading) return <FullPageSpinner />;
if (isError)   { router.replace('/'); return null; }
if (!me.onboarded && pathname !== '/onboarding') {
  router.replace('/onboarding'); return null;
}
```

Cache `me` aggressively. Invalidate on logout / onboarding completion / disconnect.

---

## 11. Local development setup

```bash
# 1. Start backend
cd ../outreach-backend
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000

# 2. Start frontend
cd ../knock-frontend  # or wherever this repo lives
cp .env.example .env.local
# NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
pnpm install
pnpm dev               # http://localhost:3000
```

Backend's `.env` must have `ALLOWED_ORIGINS=http://localhost:3000` and `FRONTEND_ORIGIN=http://localhost:3000` for CORS + redirect target.

For real Google OAuth in dev:
1. Add `http://localhost:8000/auth/google/callback` to your Google Cloud OAuth client's "Authorized redirect URIs".
2. Add yourself as a Test User on the OAuth consent screen.

---

## 12. Production deployment (Vercel)

1. Set env on Vercel:
   ```
   NEXT_PUBLIC_API_BASE_URL=https://api.<your-domain>
   ```
2. Backend `.env` (on the VM) must have:
   ```
   FRONTEND_ORIGIN=https://<your-frontend-domain>
   ALLOWED_ORIGINS=https://<your-frontend-domain>
   COOKIE_DOMAIN=.<root-domain>          # e.g. .knock.app  → covers app.knock.app + api.knock.app
   COOKIE_SECURE=true
   COOKIE_SAMESITE=none
   ```
3. Both the Next.js domain and the API must be on subdomains of the same root domain so the session cookie can be shared (`Domain=.knock.app`). If you can't share a root domain, the cookie won't ride to the API and you'd need to switch to bearer-token auth — flag this back to the backend team early.

---

## 13. Suggested folder layout

```
src/
├── app/
│   ├── layout.tsx              # root with QueryClientProvider, auth probe wrapper
│   ├── page.tsx                # / (landing)
│   ├── onboarding/page.tsx
│   ├── dashboard/page.tsx
│   ├── companies/
│   │   ├── page.tsx
│   │   └── [id]/page.tsx
│   ├── contacts/page.tsx
│   ├── templates/page.tsx
│   ├── campaigns/
│   │   ├── new/page.tsx
│   │   └── [id]/page.tsx
│   └── admin/...
│
├── components/
│   ├── ui/                     # shadcn primitives (button, dialog, ...)
│   ├── auth/AuthGate.tsx
│   ├── stats/StatCard.tsx
│   ├── tables/...
│   └── nav/Sidebar.tsx
│
├── lib/
│   ├── api.ts                  # see §5
│   ├── types.ts                # see §6
│   └── format.ts               # date/number formatters
│
└── hooks/
    ├── useMe.ts
    ├── useCampaigns.ts
    └── ...
```

---

## 14. Open questions for the frontend builder

These are knobs the frontend may legitimately decide differently from the backend's defaults — flag back if you change them:

- **Marketing landing page** — copy + design fully owned by you.
- **OAuth-error UX** — backend redirects to `${FRONTEND_ORIGIN}/connect?error=<code>` on OAuth failures (e.g. `state_mismatch`, `missing_required_scopes`, `google_oauth_unconfigured`). Build a `/connect` page that translates each code to user-friendly copy.
- **Daily-limit slider** — admin-only knob (cap = 30); plain users get the default (20). Backend enforces; UI just respects.
- **Bulk-add to campaign UX** — wizard or sticky basket? Pick what feels right; backend doesn't care.

---

## 15. What's live RIGHT NOW vs planned

**Live (you can build against today):**
- `GET /healthz`, `GET /readyz`
- `GET /auth/login`, `GET /auth/google/callback` (browser redirects)
- `GET /api/v1/auth/me`
- `POST /api/v1/auth/logout`
- `POST /api/v1/auth/disconnect`

**Planned (build placeholders / mock the response):**
- Everything else in §7.

You can build the entire frontend skeleton today against the live auth endpoints + mock data for the rest. As each backend phase ships, swap the mock for the real endpoint.

---

## 16. Where to ask questions

- Backend repo: <https://github.com/mridulchdry17/knock-backend>
- PRD: [`prd.md`](prd.md) — single source of truth for product behavior.
- Implementation status: [`prd.md` §0](prd.md) — refreshed after every backend phase.

End of frontend spec.
