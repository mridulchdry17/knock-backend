# Knock — Backend

FastAPI JSON API for **Knock**, a multi-user cold-outreach tool with a cross-user contact lock.
See [prd.md](prd.md) for the full product spec.

> Repo / package layout note: the Python package directory is `app/` (kept for stable imports). The product brand is **Knock**.

Frontend lives in a separate Next.js repo (deployed to Vercel).

## Quick start (dev)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# Fill GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, TOKEN_ENCRYPTION_KEY (see .env.example)

mkdir -p db
alembic upgrade head
python -m app.db.seed

uvicorn app.main:app --reload --port 8000
# OpenAPI: http://localhost:8000/docs
```

To run the background scheduler (sends, polling, scrapers) in dev:

```bash
RUN_SCHEDULER=1 python -m app.jobs.scheduler
```

In production these are two separate systemd units (see `deploy/systemd/`).

## Layout

```
app/
├── main.py            FastAPI factory
├── config.py          pydantic-settings
├── db/                engine, session, seed
├── core/              crypto, errors, deps, csrf
├── models/            SQLAlchemy ORM (per PRD §7)
├── schemas/           Pydantic request/response
├── routers/           HTTP endpoints
├── services/          domain logic (google, sender, moat, ...)
├── scrapers/          RSS ingestion
└── jobs/              APScheduler workers
```

## Tests

```bash
pytest
```
