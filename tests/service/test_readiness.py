"""Readiness endpoint tests.

``/health/ready`` must run every readiness check (no short-circuit), collect
all failures, and return 503 with the failed check names in ``error.details``.
A healthy service returns 200 with every check passing.
"""

from pathlib import Path

from sqlalchemy.exc import OperationalError
from unittest.mock import Mock

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS_DIR = ROOT / "hyperextract" / "service" / "migrations"


def _migration_head() -> str:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    return ScriptDirectory.from_config(config).get_current_head()


def _seed_alembic_version(repository) -> None:
    """Materialise the alembic_version table at the expected head revision.

    The in-memory test database is created via ``create_all()`` which only
    materialises the ORM tables — not the Alembic bookkeeping table. Readiness
    compares the live revision against the migration script head, so the green
    path must seed that table.
    """
    head = _migration_head()
    with repository.session_factory() as session:
        engine = session.bind
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL)"
        )
        conn.exec_driver_sql("DELETE FROM alembic_version")
        conn.exec_driver_sql(
            "INSERT INTO alembic_version (version_num) VALUES (?)", (head,)
        )


def test_ready_fails_when_database_query_fails(client, repository):
    repository.ping = Mock(side_effect=OperationalError("offline", {}, None))
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["details"][0]["check"] == "database"


def test_ready_fails_without_recent_worker(client, repository):
    repository.delete_worker_heartbeats()
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert any(
        item["check"] == "worker"
        for item in response.json()["error"]["details"]
    )


def test_ready_passes_when_all_checks_healthy(client, repository):
    repository.heartbeat_worker("worker-ready", "test")
    _seed_alembic_version(repository)
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert all(body["checks"].values())
