"""Static tests for the hardened service Docker image and build context.

These tests read the Dockerfile and .dockerignore as text and assert on
content. They do NOT build the image, so they can run in CI without Docker.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_image_is_lockfile_based_and_non_root():
    text = (ROOT / "docker/service.Dockerfile").read_text()
    assert "ghcr.io/astral-sh/uv:" in text
    assert "uv sync --frozen" in text
    assert "USER 10001:10001" in text
    assert "pip install" not in text


def test_context_excludes_secrets_and_runtime_data():
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert ".env" in ignored
    assert ".git/" in ignored
    assert "exchange/" in ignored


def test_compose_has_migration_gate_and_database_volume(compose):
    services = compose["services"]
    assert services["he-migrate"]["command"] == [
        "uv",
        "run",
        "--no-sync",
        "alembic",
        "upgrade",
        "head",
    ]
    assert (
        services["he-api"]["depends_on"]["he-migrate"]["condition"]
        == "service_completed_successfully"
    )
    assert (
        services["he-worker"]["depends_on"]["he-migrate"]["condition"]
        == "service_completed_successfully"
    )
    assert "postgres-data:/var/lib/postgresql/data" in services["postgres"]["volumes"]
    assert (
        services["postgres"]["environment"]["POSTGRES_PASSWORD"]
        == "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}"
    )


def test_compose_migration_is_one_shot_and_postgres_unpublished(compose):
    services = compose["services"]
    # he-migrate must never restart — it runs once and exits.
    assert services["he-migrate"]["restart"] == "no"
    # he-migrate must wait for postgres to be healthy before applying migrations.
    assert (
        services["he-migrate"]["depends_on"]["postgres"]["condition"]
        == "service_healthy"
    )
    # API and Worker commands must be ONLY the entrypoint binary — no inline
    # `alembic upgrade head` chaining, since he-migrate owns migrations.
    assert services["he-api"]["command"] == ["uv", "run", "--no-sync", "he-api"]
    assert services["he-worker"]["command"] == ["uv", "run", "--no-sync", "he-worker"]
    # PostgreSQL must never publish ports to the host.
    assert "ports" not in services["postgres"]
    # postgres-data volume must be declared at the top level.
    assert "postgres-data" in compose["volumes"]
