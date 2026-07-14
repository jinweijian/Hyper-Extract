from __future__ import annotations

import os
from datetime import timedelta, timezone
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends

from hyperextract.service.db_models import utcnow
from hyperextract.service.errors import ServiceError
from hyperextract.service.runtime import ServiceRuntime

from ..dependencies import get_runtime

router = APIRouter()

# The profile the API validates during readiness. It must match the Worker's
# default profile so both processes agree on the secret-free fingerprint.
DEFAULT_PROFILE_NAME = "openai-compatible-default"

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _expected_migration_head() -> str | None:
    """Return the head revision from the migration scripts, or ``None``.

    Computed from the shipped ``migrations/`` directory so it stays in sync
    with new migration files without a hardcoded constant.
    """
    try:
        config = Config()
        config.set_main_option("script_location", str(_MIGRATIONS_DIR))
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:
        return None


def _check_database(runtime: ServiceRuntime) -> bool:
    try:
        runtime.repository.ping()
        return True
    except Exception:
        return False


def _check_migration(runtime: ServiceRuntime) -> bool:
    expected = _expected_migration_head()
    if expected is None:
        return False
    try:
        return runtime.repository.current_migration_revision() == expected
    except Exception:
        return False


def _check_package_root(runtime: ServiceRuntime) -> bool:
    root = runtime.settings.package_root
    return root.is_dir() and os.access(root, os.R_OK)


def _check_run_root(runtime: ServiceRuntime) -> bool:
    root = runtime.settings.run_root
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".ready-probe"
        with probe.open("wb") as handle:
            handle.write(b"ok")
            handle.flush()
            os.fsync(handle.fileno())
        probe.unlink()
        return True
    except Exception:
        return False


def _check_model_profiles(runtime: ServiceRuntime) -> bool:
    try:
        runtime.model_profiles.public_descriptor(DEFAULT_PROFILE_NAME)
        return True
    except Exception:
        return False


def _check_worker(runtime: ServiceRuntime) -> bool:
    try:
        latest = runtime.repository.latest_worker_heartbeat()
    except Exception:
        return False
    if latest is None:
        return False
    # SQLite returns naive datetimes on read-back even when stored with
    # timezone info; normalise to aware UTC so the comparison is well-defined.
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    max_age = timedelta(seconds=2 * runtime.settings.heartbeat_seconds)
    return latest >= utcnow() - max_age


# Ordered so the most fundamental dependencies are reported first.
_CHECKS = (
    ("database", _check_database),
    ("migration", _check_migration),
    ("package_root", _check_package_root),
    ("run_root", _check_run_root),
    ("model_profiles", _check_model_profiles),
    ("worker", _check_worker),
)


@router.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
def ready(runtime: ServiceRuntime = Depends(get_runtime)) -> dict:
    """Run every readiness check and collect all failures.

    Never short-circuits: every check runs so the response lists every problem
    at once. Failed check names are returned in ``error.details``; no database
    URLs or secret values are ever included.
    """
    results = {name: check(runtime) for name, check in _CHECKS}
    failed = [name for name, ok in results.items() if not ok]
    if failed:
        raise ServiceError(
            503,
            "SERVICE_NOT_READY",
            "Service readiness checks failed",
            details=[{"check": name} for name in failed],
        )
    return {"status": "ready", "checks": results}
