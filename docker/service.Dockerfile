# syntax=docker/dockerfile:1.7
#
# HyperExtract service image — hardened, lockfile-based, non-root.
#
# Build stage uses a version-pinned `ghcr.io/astral-sh/uv` image to resolve
# dependencies strictly from `uv.lock` (no pip, no network resolution).
# Runtime stage is a minimal `python:3.11-slim` image running as UID/GID 10001.

# ===== Build stage: resolve dependencies with pinned uv =====
# Keep this tag explicit so rebuilding the same commit cannot silently change
# the uv resolver/install implementation.
FROM ghcr.io/astral-sh/uv:0.9.26-python3.11-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Copy lockfile and project metadata first for layer cache reuse.
COPY pyproject.toml uv.lock ./
COPY hyperextract ./hyperextract
COPY alembic.ini README.md ./

# Sync strictly from the lockfile. No dev dependencies, only the
# `service` and `graph-rag` extras. `--frozen` forbids lockfile mutation.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra service --extra graph-rag

# ===== Runtime stage: slim Python image =====
FROM python:3.11.15-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Create the non-root service user with fixed UID/GID 10001.
RUN groupadd --gid 10001 hyperextract && \
    useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash hyperextract

# Copy the resolved virtual environment and application source from the builder.
COPY --from=builder --chown=10001:10001 /app /app

# Install the entrypoint and ensure /exchange exists and is owned by 10001.
# The entrypoint only validates readability — it does NOT chown at runtime
# and does NOT run database migrations.
COPY docker/entrypoint.sh /usr/local/bin/he-entrypoint
RUN chmod +x /usr/local/bin/he-entrypoint && \
    mkdir -p /exchange && chown 10001:10001 /exchange

USER 10001:10001
EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/he-entrypoint"]
CMD ["he-api"]
