# Knock — Shared Memory (Frontend ↔ Backend)

> Living doc that both repos read from. Update on every notable change to either side, every infra decision, every "we agreed to do X" moment in chat. The goal: someone (or any LLM) walking into either repo cold can read this file and know where the project actually is, not where the PRD says it should be.
>
> **Format rule:** When you make a change, append a dated line to the relevant section *and* update the "Status snapshot" at the top. Don't rewrite history — old entries stay so we can trace decisions back.

---

## Status snapshot (last updated: 2026-05-03)

| Area | Status |
|---|---|
| Frontend repo | Live at `github.com/mridulchdry17/knock-frontend`, branch `main`. Not yet connected to Vercel. |
| Frontend UI | Single landing page with email-only waitlist form. No other pages yet. |
| Frontend → Backend wiring | Same-origin Next.js proxy at `/api/waitlist` → `${BACKEND_URL}/api/v1/waitlist`. Backend hostname kept out of the browser bundle. |
| Backend repo | Live at `github.com/mridulchdry17/knock-backend`. Active dev branch: `feat/waitlist-and-turso` (waitlist endpoint + Turso support). Earlier branches: `docs/progress-and-frontend-spec`, `feat/google-oauth-and-sessions`, `feat/foundation-and-schema`. |
| Backend deployment | **Not yet deployed.** VM provisioned but uvicorn not running. |
| Database | Turso (libSQL) — `knock-mridulchdry17.aws-ap-south-1.turso.io`. Schema migrated (10 tables incl. `waitlist`, all empty in prod). 2 dev test rows present. |
| Infra — Azure VM | Provisioned, public IP `40.82.143.50`, DNS label `knock-api.koreacentral.cloudapp.azure.com`. NSG port 8000 status unverified by user. Backend not running on it yet. |
| Infra — Vercel | Frontend not yet imported. Will set env var `BACKEND_URL` once VM is live. |
| OAuth / Gmail / sending | Not started. PRD Phase 2+ work — deferred until waitlist is live. |

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

---

## What's next (in order)

1. **Verify NSG port 8000 is open** in the Azure portal (user task).
2. **Deploy backend on the VM**: SSH in, clone repo, install deps, point `.env` at Turso, run `alembic upgrade head` (no-op on Turso since schema is there, but safe to re-run), start uvicorn (foreground first, then systemd unit `deploy/systemd/knock-api.service` after edits for `User=azureuser` and `/home/azureuser/...` paths).
3. **From laptop**: `curl http://knock-api.koreacentral.cloudapp.azure.com:8000/healthz` should return `{"status":"ok"}`.
4. **Import frontend to Vercel**, set env var `BACKEND_URL=http://knock-api.koreacentral.cloudapp.azure.com:8000`, deploy.
5. **Submit a real email on the live URL**, verify row appears in Turso (web UI or `turso db shell knock` → `select * from waitlist;`).
6. **Cut release**: merge `feat/waitlist-and-turso` to backend `main`, merge frontend to `main`. Open dev branch for OAuth/campaigns.

Stretch (after waitlist is live):
- Buy domain (~$8/year) → switch backend to `api.knock.<tld>` → start OAuth work on backend dev branch.
- Add TLS to backend with Caddy + Let's Encrypt (uses the Azure DNS label or the new domain).
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

---

## How to keep this file fresh

- After any infra change (URL, region, DNS, env var) → update the relevant section + add a dated entry to "Conversation log".
- After any new endpoint or schema change → update the "Endpoints" or "Schema" section + log it.
- After any "we discussed and decided X" moment → append to "Conversation log" with one paragraph of context. Don't trust memory; Claude rotates between sessions.
- When adding a new branch, deployment, or service → mention it in "Status snapshot" so the top of the file is always the source of truth for "where are we right now".
- This file is committed to the **backend** repo (canonical home — most coordination concerns the backend) but the frontend should `git submodule` or simply mirror it via copy-paste when major updates land. Easier path: link to it from the frontend README.
