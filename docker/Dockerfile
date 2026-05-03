FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates\
    && rm -rf /var/lib/apt/lists/*

ARG INTERNAL_GITLAB_HOST=gitlab.example.internal
RUN openssl s_client -connect "${INTERNAL_GITLAB_HOST}:443" -showcerts </dev/null 2>/dev/null \
        | openssl x509 -outform PEM \
        > /usr/local/share/ca-certificates/gitlab-internal.crt \
    && update-ca-certificates

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

# Install dependencies first (cached layer, unaffected by app code changes).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY alembic.ini ./
COPY argus ./argus
COPY migrations ./migrations

# ruff powers the linter pre-filter (argus/review/linters.py); it degrades
# gracefully when absent, but prod should have it.
# graphify (from the graphifyy package) is a declared runtime dependency
# above, so `uv sync --frozen --no-dev` already installs its CLI binary.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev && \
    uv pip install --python /usr/local/bin/python ruff

EXPOSE 8100

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn argus.api.app:app --host 0.0.0.0 --port 8100"]
