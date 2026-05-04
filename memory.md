# Knock — Shared Memory (Frontend ↔ Backend)

> Living doc that both repos read from. Update on every notable change to either side, every infra decision, every "we agreed to do X" moment in chat. The goal: someone (or any LLM) walking into either repo cold can read this file and know where the project actually is, not where the PRD says it should be.
>
> **Format rule:** When you make a change, append a dated line to the relevant section *and* update the "Status snapshot" at the top. Don't rewrite history — old entries stay so we can trace decisions back.

---

## Status snapshot (last updated: 2026-05-05)

| Area | Status |
|---|---|
| Frontend repo | Live at `github.com/mridulchdry17/knock-frontend`, deployed on Vercel. |
| Frontend UI | Single landing page with email-only waitlist form. |
| Frontend → Backend wiring | Same-origin Next.js proxy at `/api/waitlist` → `${BACKEND_URL}/api/v1/waitlist`. Backend hostname kept out of the browser bundle. |
| Backend repo | Live at `github.com/mridulchdry17/knock-backend`. Active dev branch: `feat/auth-bearer-tokens`. Waitlist + Turso work merged to `main`. |
| Backend deployment | **Live** on Azure VM at `http://knock-api.koreacentral.cloudapp.azure.com:8000`. systemd unit running. HTTP only — TLS planned (Phase 3, Caddy + Let's Encrypt). |
| Database | Turso (libSQL) — `knock-mridulchdry17.aws-ap-south-1.turso.io`. Schema migrated (10 tables incl. `waitlist`). Real signups landing in production. |
| Auth model | **Bearer tokens**, not cookies (Phase 2, branch `feat/auth-bearer-tokens`). Session token = raw `sessions.id`, transported via `Authorization: Bearer <token>`. OAuth callback redirects to `${FRONTEND_ORIGIN}/auth/complete?next=...#token=...` — fragment never reaches the server. CSRF middleware deleted (no cookies = no CSRF). `oauth_state` cookie remains for the OAuth round-trip (same-origin, backend-only). Frontend changes live in knock-frontend repo. |
| Three user tiers | Planned: `free` / `paid` / `super_admin`. Schema column + tier-gating dependencies arrive in Phase 4 (not yet built). Existing `users.is_admin` boolean unchanged for now. |
| Billing | **Deferred to v3.** No Stripe in v1/v2. Tier transitions are manual via super_admin endpoints once Phase 4 ships. |
| Infra — Azure VM | Public IP `40.82.143.50`, DNS label `knock-api.koreacentral.cloudapp.azure.com`. NSG 8000 open. |
| OAuth / Gmail / sending | OAuth routes wired, unused in production. Real OAuth UX + Gmail send pipeline = Phase 5. |

---

## What "done" means right now

The MVP we're racing toward is **a waitlist landing page that captures emails to a durable DB**. Everything else is parked.

Once the page is live on Vercel and submissions persist to Turso, we cut a release and start the dev branch for the real product (OAuth, campaigns, etc.).

---

## Architecture (current, not the PRD's eventual)

```
User browser
   │ HTTPS
   ▼
Vercel — Next.js App Router
   • app/page.tsx              landing page
   • app/waitlist-form.tsx     email form, calls /api/waitlist (same origin)
   • app/api/waitlist/route.ts server-side proxy → ${BACKEND_URL}/api/v1/waitlist
   • env: BACKEND_URL          server-only, NOT NEXT_PUBLIC_, never in client bundle
   │
   │ HTTP (server-to-server, mixed-content rule doesn't apply)
   ▼
Azure VM — uvicorn :8000
   • FastAPI app (knock-backend)
   • POST /api/v1/waitlist     public, no auth, idempotent, validates email
   • CSRF middleware           requires X-Requested-With on non-GET to /api/v1/*
   • DATABASE_URL              points at Turso (sqlite+libsql://...)
   │
   │ HTTPS (Hrana over HTTPS)
   ▼
Turso — managed libSQL (region: ap-south-1, Mumbai)
   • all data lives here, not on the VM
   • VM is stateless, can be nuked + rebuilt without data loss
```

### Key architectural choices (and why)

- **Same-origin proxy on Vercel instead of direct browser→backend**: hides the backend hostname from the browser bundle (no `NEXT_PUBLIC_API_BASE_URL` exists). Also sidesteps the HTTPS-frontend → HTTP-backend mixed-content block, since browser only ever talks to Vercel HTTPS. Trade-off: when OAuth is added later, the OAuth redirect chain *cannot* go through this proxy (cookies are host-scoped) — at that point we'll need a real domain so frontend + backend can be sibling subdomains. ~$8/year, deferred until then.
- **Turso instead of SQLite-on-VM**: VM rebuilds were a real risk for the launch list. Turso = managed libSQL, drop-in for SQLite (same dialect), 9 GB free tier, no auto-pause-after-inactivity. Picked Turso over Supabase because Supabase pauses free DBs after 7 days idle. Picked Turso over Neon for now because the existing SQLAlchemy code + Alembic migrations work as-is — no SQLite→Postgres migration needed today. Will revisit if we hit SQLite's single-writer concurrency wall around 50 concurrent users (per PRD §5).
- **Azure DNS-name label instead of buying a domain**: `*.cloudapp.azure.com` is free, stable, and good enough for the waitlist phase. We'll buy a real domain when OAuth lands.

---

## Endpoints (current)

| Method | Path | Auth | Purpose | Lives on |
|---|---|---|---|---|
| GET | `/healthz` | — | Liveness probe | backend |
| GET | `/readyz` | — | Liveness + DB ping | backend |
| POST | `/api/v1/waitlist` | **public** | Email-only join. Idempotent — duplicate returns `{ok:true}`, no presence-leak. | backend |
| GET | `/auth/login` | browser-redirect | Google OAuth bootstrap | backend (works, unused) |
| GET | `/auth/google/callback` | browser-redirect | OAuth return | backend (works, unused) |
| GET | `/api/v1/auth/me` | session | Current user | backend (works, unused) |
| POST | `/api/v1/auth/logout` | session | Clear session | backend (works, unused) |
| POST | `/api/v1/auth/disconnect` | session | Revoke Google + clear all sessions | backend (works, unused) |
| POST | `/api/waitlist` | — | Same-origin **proxy** that forwards to backend. Validates email locally, sets `X-Requested-With`, hides backend URL. | frontend (Vercel) |

Everything else in [PRD §14](prd.md) is planned, not built.

---

## Database — schema (Turso)

10 tables, all migrated. Migrations in `alembic/versions/`:
- `0001_init` — users, sessions, companies, contacts, templates, campaigns, send_queue, user_contact_map, global_contact_lock, email_logs
- `0002_waitlist` — waitlist (id INT PK, email TEXT UNIQUE NOT NULL, created_at TIMESTAMP)

`waitlist` is intentionally minimal: just email + timestamp. The plan if/when we want soft-gate launch:
- New OAuth users: callback checks `WaitlistEntry.exists(email=oauth_email)`. Match → straight in. No match → redirect to onboarding with a "what email did you sign up with?" extra field.
- One nullable column on `users` (`waitlist_email TEXT`) to remember the gate-pass. Not added yet.

Decision recorded: **don't store IP/user_agent/source on waitlist rows.** Keep it the absolute minimum. Easy to ALTER later if needed.

---

## Configuration / env vars

### Backend `.env` (gitignored)
```
APP_ENV=development
DATABASE_URL=sqlite+libsql://knock-mridulchdry17.aws-ap-south-1.turso.io/?authToken=<JWT>&secure=true
TOKEN_ENCRYPTION_KEY=<Fernet key, generated once>
FRONTEND_ORIGIN=...
ALLOWED_ORIGINS=...
GOOGLE_CLIENT_ID / SECRET / REDIRECT_URI=    # blank for now, OAuth not used
```

The `sqlalchemy-libsql` dialect needs:
- `secure=true` in URL → forces `https://` scheme (Turso 405s on http).
- `authToken` extracted to `connect_args["auth_token"]` by `app/db/base.py` (the dialect itself doesn't pass query-string tokens through cleanly to libsql_experimental).
- `isolation_level="AUTOCOMMIT"` on the engine — Turso's Hrana protocol over HTTPS doesn't speak `BEGIN`/`ROLLBACK`. ORM `Session.commit()` still works (flush + autocommit at connection level).
- A small monkey-patch on `SQLiteDialect_libsql.get_isolation_level/set_isolation_level` because the inherited stdlib SQLite dialect runs `PRAGMA read_uncommitted` during `initialize()` and Turso 405s on it.

All of that lives in `app/db/base.py`. Local dev with file SQLite still works — the patches only fire when `DATABASE_URL` starts with `sqlite+libsql:`.

### Frontend `.env.local` (gitignored)
```
BACKEND_URL=http://localhost:8000        # dev
# Production: set on Vercel dashboard, NOT in any committed file:
# BACKEND_URL=http://knock-api.koreacentral.cloudapp.azure.com:8000
```

**Important:** `BACKEND_URL` is server-only. There is **no** `NEXT_PUBLIC_*` env var for the API URL — the goal is for the backend hostname to never reach the browser bundle.

---

## Infrastructure (current)

### Azure VM
- Public IP: `40.82.143.50` (static)
- DNS label: `knock-api.koreacentral.cloudapp.azure.com`
- Region: Korea Central
- Resource: VM size unknown (B-series 1 GB target per PRD)
- NSG inbound 8000/TCP: **status unverified by user** (need to confirm before deploy)
- SSH user: `azureuser`
- uvicorn / systemd: **not yet set up** on the VM
- Linux UFW: status unverified

### Turso
- Org: `mridulchdry17`
- DB name: `knock`
- Region: `aws-ap-south-1` (Mumbai)
- URL: `libsql://knock-mridulchdry17.aws-ap-south-1.turso.io`
- Auth token: stored in backend `.env` only. **Never in git, never in chat history.** Rotate via `turso db tokens create knock --expiration none` if leaked.
- 2 test rows in `waitlist` from local dev runs (cleanup optional, doesn't hurt).

### Vercel
- Frontend repo not yet imported.
- Plan: import `mridulchdry17/knock-frontend`, set `BACKEND_URL` env var to the Azure VM hostname, deploy.

---

## Conversation log — important decisions / outcomes

Append to this section when something material happens. Date + one-paragraph summary. Don't condense — future-us needs the context.

**2026-05-03 — Initial waitlist build.**
Goal locked: pre-launch waitlist landing only, nothing else of the product yet. Frontend = Next.js 14 App Router, single page. Backend = FastAPI route added at `POST /api/v1/waitlist` (model + migration + schema + repo + router) — public, no auth, idempotent, returns ok=true on duplicates so the endpoint isn't an existence oracle. CSRF middleware still applies (requires X-Requested-With from the proxy). Frontend + backend repos pushed to GitHub via personal SSH key alias `github-personal` in `~/.ssh/config`. No Claude co-author on commits.

**2026-05-03 — Backend hostname hiding.**
User flagged that `NEXT_PUBLIC_API_BASE_URL` would expose the VM URL in the JS bundle. Switched to same-origin proxy (`app/api/waitlist/route.ts` on Vercel forwards to `BACKEND_URL`). Side benefit: HTTPS-frontend → HTTP-backend works because the mixed-content rule only applies to browser fetches, not server-to-server. Verified by grepping the production JS bundles — no `localhost:8000` or backend hint anywhere.

**2026-05-03 — Domain decision deferred.**
User has no custom domain, using Vercel's free `*.vercel.app`. With no domain we can't do the proper `app.knock.app` + `api.knock.app` cookie-sharing setup that OAuth needs ([FRONTEND.md §12](FRONTEND.md)). Agreed to defer that until OAuth lands; for now the proxy + Azure DNS-name label is enough. Cost to fix later: ~$8/year for a Namecheap/Porkbun domain.

**2026-05-03 — Azure VM (not Hetzner).**
PRD docs Hetzner CX11; user actually has Azure. Static public IP `40.82.143.50`, free DNS label set to `knock-api.koreacentral.cloudapp.azure.com`. NSG (Azure firewall) needs port 8000 inbound rule before backend is reachable. UFW status unknown.

**2026-05-03 — Switched DB to Turso.**
SQLite-on-VM was the original plan (PRD MVP) but VM rebuilds would erase the launch list. Picked Turso over Supabase (which pauses idle free DBs after 7 days) and over Neon (would force SQLite→Postgres migration today). Required code changes in `app/db/base.py`: extract `auth_token` to connect_args, force AUTOCOMMIT, monkey-patch `get_isolation_level` because Turso's Hrana protocol over HTTPS rejects `PRAGMA read_uncommitted` and `BEGIN`/`ROLLBACK`. Local dev with file SQLite is still supported via URL prefix detection.

**2026-05-03 — Soft-gate plan for OAuth.**
Decided that when OAuth ships, we'll do a "soft gate": OAuth callback creates the user normally, then if their Gmail address isn't on the waitlist, the onboarding page shows an extra field "what email did you sign up with?". One nullable `waitlist_email` column on `users` to remember the gate-pass. Not implemented yet.

**2026-05-03 — Email volume sanity check.**
At PRD's 100-user verification cap and a year of activity, projected DB size is ~590 MB — well under Turso's 9 GB free tier. The actual ceiling is SQLite's single-writer concurrency (PRD §5), not Turso's storage. Migration to Postgres (Neon) is a planned step at ~50 concurrent users. Nothing to do now.

**2026-05-03 — Walked back the no-presence-leak design on the waitlist endpoint.**
Originally `POST /api/v1/waitlist` returned `200 {ok:true}` for both new and duplicate signups specifically so the endpoint couldn't be used as a presence oracle. We changed this: duplicate now returns `409 {error:{code:"already_registered", ...}}`. Reasoning: this is a public marketing waitlist, not a privacy-sensitive list — every well-known waitlist (Mailchimp, Substack, Notion, etc.) tells you "you're already subscribed." Without that signal, repeat-submitters re-fire network calls + double-count in Vercel Analytics + don't get the closure-feedback that they're already in. Frontend now shows a distinct "You're already on the list" success card on 409 vs the regular "You're on the list" card on 200. Also added Vercel Analytics `track('waitlist_signup', {domain})` only on the genuine 200 path so the conversion count is unique signups (not re-submits). The DB UNIQUE constraint was already enforcing data integrity — this change is purely about UX + analytics cleanliness.

**2026-05-03 — OAuth auth model: switch from cookies to bearer tokens (decision pending).**
Re-examined the cookie-based session model when user pointed out we don't have a domain. The PRD's design uses an HTTP-only `session` cookie set on the backend domain, scoped to a parent domain (`Domain=.knock.app`) so both `app.knock.app` (frontend) and `api.knock.app` (backend) receive it. **This requires a domain we own** — `*.vercel.app` and `*.cloudapp.azure.com` can't share cookies because (a) we don't own the parent and (b) both are on the Public Suffix List, which browsers explicitly forbid setting `Domain=` cookies on. DuckDNS has the same PSL problem.

User's lean: **switch to bearer tokens** so we never need a domain. Plan when OAuth phase begins:
- Backend issues an opaque session token (or signed JWT) at the end of OAuth callback, returns it in JSON response body instead of `Set-Cookie`.
- Frontend stores it (in `localStorage` or, better, in a closure/in-memory + `sessionStorage` for less XSS surface).
- Frontend sends it on every API call as `Authorization: Bearer <token>`.
- `app/core/deps.py::get_current_user` reads from `Authorization` header instead of `Cookie`.
- The `sessions` table stays — it's still the source of truth, just keyed by the bearer token instead of a cookie value.
- Drop the CSRF middleware entirely (no cookies = no CSRF). The `X-Requested-With` requirement on `/api/v1/*` becomes unnecessary; we'd remove it.
- Logout = delete the session row (same as today) + frontend wipes its stored token.
- Backend changes affected: `app/routers/auth.py` (callback returns JSON not redirect, or redirects with token in URL fragment), `app/core/deps.py` (read header), `app/core/csrf.py` (delete or no-op), `app/core/cookies.py` (delete or shrink). Frontend gains a small token-store module + Authorization-header injection in its API client.

**Risks acknowledged:** bearer-in-localStorage is XSS-vulnerable (any JS injection grabs the token); can't be `httpOnly`; harder to invalidate centrally (we still have the sessions table so it's revocable, just slower than cookie-clearing). Mitigations: strict CSP, rotate tokens on refresh, short TTL with refresh tokens. We accept this trade as the cost of not buying a domain.

**Open until OAuth phase begins.** Until then nothing changes — the waitlist endpoint doesn't authenticate, so cookies/bearer doesn't matter.

**2026-05-05 — Bearer-token swap implemented (branch `feat/auth-bearer-tokens`).**
Phase 2 from the roadmap. Concrete changes:
- `app/core/deps.py` — `get_current_user` now reads `Authorization: Bearer <token>` via FastAPI's `HTTPBearer` security scheme. New dep `CurrentSessionToken` exposes the raw token to logout/disconnect. Bearer token IS the `sessions.id` (no hashing change in this PR; matches existing scheme).
- `app/core/cookies.py` — dropped `set_session_cookie`/`clear_session_cookie`/`SESSION_COOKIE`. Kept `oauth_state` helpers — that cookie is same-origin (backend domain only) and required to bind the OAuth round-trip.
- `app/core/csrf.py` — **deleted**. No cookies = no CSRF surface.
- `app/main.py` — removed `CSRFHeaderMiddleware`. CORS dropped `X-Requested-With` from allow-headers, flipped `allow_credentials=False` (correct now that we're not sending cookies cross-origin).
- `app/routers/auth.py` — `/auth/google/callback` redirects to `${FRONTEND_ORIGIN}/auth/complete?next=<onboarding|dashboard>#token=<session.id>`. Fragment is browser-only, never reaches the server, doesn't appear in access logs or `Referer` headers. Logout/disconnect take `CurrentSessionToken` instead of `Cookie(SESSION_COOKIE)`.
- `.github/workflows/ci.yml` — first CI workflow: ruff check + import smoke + `alembic upgrade head` against ephemeral SQLite. Pytest step is conditional (skips if no `tests/test_*.py`). Required before merge.
- Pre-existing lint errors fixed (unused imports, sorted `__all__`, `contextlib.suppress` rewrite). Ruff now clean.

Tier-gating helpers (`require_tier`, `require_super_admin`, `require_paid`) **deferred to Phase 4** PR — they need `users.tier` column which doesn't exist yet. Adding them now would be dead code referencing a missing attribute.

Frontend changes (token store in sessionStorage, `Authorization` header injection, `/auth/complete` route) live in the knock-frontend repo — separate PR.

---

## What's next (in order)

1. **Verify NSG port 8000 is open** in the Azure portal (user task).
2. **Deploy backend on the VM**: SSH in, clone repo, install deps, point `.env` at Turso, run `alembic upgrade head` (no-op on Turso since schema is there, but safe to re-run), start uvicorn (foreground first, then systemd unit `deploy/systemd/knock-api.service` after edits for `User=azureuser` and `/home/azureuser/...` paths).
3. **From laptop**: `curl http://knock-api.koreacentral.cloudapp.azure.com:8000/healthz` should return `{"status":"ok"}`.
4. **Import frontend to Vercel**, set env var `BACKEND_URL=http://knock-api.koreacentral.cloudapp.azure.com:8000`, deploy.
5. **Submit a real email on the live URL**, verify row appears in Turso (web UI or `turso db shell knock` → `select * from waitlist;`).
6. **Cut release**: merge `feat/waitlist-and-turso` to backend `main`, merge frontend to `main`. Open dev branch for OAuth/campaigns.

Stretch (after waitlist is live):
- **Switch auth model from cookies → bearer tokens** *before* implementing real OAuth (see 2026-05-03 OAuth log entry). Touches `app/routers/auth.py`, `app/core/deps.py`, `app/core/csrf.py`, `app/core/cookies.py`, and the frontend API client. Roughly 1 day. Lets us skip buying a domain.
- ~~Buy domain (~$8/year)~~ → no longer needed if bearer-token plan holds. Revisit only if we change our minds about XSS risk on localStorage.
- Add TLS to backend with Caddy + Let's Encrypt (uses the Azure DNS label `knock-api.koreacentral.cloudapp.azure.com` — works without a custom domain).
- Write the soft-gate logic on the OAuth callback.
- Pull the waitlist CSV when ready to email the launch announcement (use Knock itself once OAuth is live — eat the dog food).

---

## Things that bit us / non-obvious traps

- **`PRAGMA read_uncommitted` over Turso → 405.** Solution in `app/db/base.py`. Applies only to the libsql-driver path.
- **`BEGIN`/`ROLLBACK` over Turso → 405.** Solved with `isolation_level="AUTOCOMMIT"` on the engine. ORM Session still works because flush+commit at session level translates to autocommitted writes at connection level.
- **`secure=true` URL param is required for libsql.** Without it the dialect defaults to `http://` and Turso 405s every request. This is non-obvious; fix is in `_split_libsql_url` (we keep `secure=true` in URL but lift `authToken` into connect_args).
- **Alembic builds its own engine.** Initial implementation of `alembic/env.py` did `engine_from_config(...)` which bypassed our `_build_engine()` and dropped the `auth_token` connect_arg. Fixed by importing and reusing `engine` from `app.db.base`.
- **Vercel `NEXT_PUBLIC_*` vars are baked into the client JS bundle.** Used to leak the backend URL. Fixed by switching to the proxy with a server-only `BACKEND_URL`. Verified via grep across `_next/static/chunks/*.js`.
- **Azure VMs block ICMP by default.** `ping <hostname>` fails even when the host is up. Don't use ping to check liveness — use `curl :22` for SSH or `curl :8000/healthz` for the backend.
- **`isolation_level="AUTOCOMMIT"` doesn't auto-commit ad-hoc `engine.connect().execute(...)` on libsql.** Despite the engine option, raw DML through `engine.connect()` silently rolls back when the connection closes — the row appears deleted in that connection's view but the change never reaches Turso. **Workaround:** use `with engine.begin() as c:` for any ad-hoc DML from the shell — this opens an explicit transaction and commits on context-manager exit. The app's normal request flow is unaffected because `app/routers/*` use the SQLAlchemy `Session` and call `db.commit()` explicitly, which works correctly.

---

## How to keep this file fresh

- After any infra change (URL, region, DNS, env var) → update the relevant section + add a dated entry to "Conversation log".
- After any new endpoint or schema change → update the "Endpoints" or "Schema" section + log it.
- After any "we discussed and decided X" moment → append to "Conversation log" with one paragraph of context. Don't trust memory; Claude rotates between sessions.
- When adding a new branch, deployment, or service → mention it in "Status snapshot" so the top of the file is always the source of truth for "where are we right now".
- This file is committed to the **backend** repo (canonical home — most coordination concerns the backend) but the frontend should `git submodule` or simply mirror it via copy-paste when major updates land. Easier path: link to it from the frontend README.
