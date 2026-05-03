# argus

argus is a GitLab merge-request review platform being built from scratch.
This repository currently contains a **read-only ingestion skeleton**: a GitLab
client, an idempotent payload normalizer, a cursor-based sync poller, and a
minimal read API. There is no review/LLM logic yet.

## Requirements

- Python 3.12
- Docker (for running Postgres locally)

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

docker compose up -d db
```

`docker compose up -d db` starts Postgres 16 with the `pgvector` extension,
exposed on **port 5433** (not the default 5432, to avoid clashing with a local
Postgres install). It also runs a committed init script
(`docker/initdb.d/00-role-search-path.sql`) on first container start that
pins the `argus` role's `search_path` to `public`. Without this, Postgres'
default search path resolves the `"$user"` schema first, which makes Alembic
see spurious drift between reflected and declared schemas on every
`alembic check` / `alembic revision --autogenerate`, even with no real changes.

## Running tests

```bash
pytest -v
```

The test suite creates a `argus_test` database automatically (via an
admin connection) before the run and tears it down after — no manual database
setup is required beyond having the `db` container running.

## Database migrations

Migrations are managed with Alembic and read the database URL from the
`ARGUS_DATABASE_URL` environment variable (see `migrations/env.py`).

```bash
export ARGUS_DATABASE_URL=postgresql+asyncpg://argus:argus@localhost:5433/argus
alembic upgrade head
alembic check
```

`alembic check` verifies that the current ORM models (`argus.domain.models`)
and the applied migrations agree — i.e. there is no unmigrated model drift.

## Running the API locally

```bash
uvicorn argus.api.app:app --port 8100
```

Key environment variables (all read via `argus.config.Settings`, prefix `ARGUS_`):

- `ARGUS_DATABASE_URL` — Postgres connection string (asyncpg driver)
- `ARGUS_GITLAB_URL` — base URL of the GitLab instance to ingest from
- `ARGUS_GITLAB_TOKEN` — GitLab personal/project access token
- `ARGUS_GITLAB_SSL_VERIFY` — set to `false` to disable TLS verification (e.g. for self-signed internal GitLab instances)
- `ARGUS_POLLER_ENABLED` — set to `true` to start the background sync poller on app startup (requires `ARGUS_GITLAB_TOKEN` to be set)

See `.env.example` for a sample configuration.

## Runtime settings

Non-secret settings can be overridden at runtime via the database, allowing live configuration changes without restarting the API. Settings precedence (highest to lowest):

1. **Database** (`runtime_settings` table) — set via `PUT /settings` from the UI
2. **Environment variables** (`ARGUS_*` prefix) — bootstrap defaults, always available
3. **Code defaults** — hardcoded application defaults

The `GET /settings` and `PUT /settings` routes expose all non-secret settings (secret keys like `gitlab_token`, `api_token`, `distiller_api_key`, `langfuse_public_key`, and `langfuse_secret_key` are never returned and never editable via the UI). Secrets remain environment-only: the API returns only a `secret_present` boolean for each secret, allowing the UI to show which credentials are configured without exposing them.

Settings are loaded once per job/poller cycle, so running reviews keep the settings they started with.
