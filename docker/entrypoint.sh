#!/bin/sh
# HyperExtract service container entrypoint.
#
# Responsibilities (intentionally minimal):
#   1. Validate that /exchange is readable by the non-root service user.
#   2. Hand off to the container command via `exec "$@"`.
#
# This entrypoint deliberately does NOT:
#   - Run database migrations (alembic upgrade). Migrations are an explicit
#     operational step owned by the deployer, not the container runtime.
#   - Recursively chown volumes. Volume ownership is established at image
#     build time and via the orchestrator's volume mounts.
#   - Source any secret-bearing files.

set -eu

# /exchange is mounted at runtime; it must be readable by UID 10001.
if [ -d /exchange ] && [ ! -r /exchange ]; then
    echo "he-entrypoint: /exchange exists but is not readable by UID $(id -u)" >&2
    exit 1
fi

exec "$@"
