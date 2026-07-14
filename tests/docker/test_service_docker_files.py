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


def test_api_has_no_secrets_or_egress_and_worker_does(compose):
    api = compose["services"]["he-api"]
    worker = compose["services"]["he-worker"]
    assert "model-egress" not in api["networks"]
    assert "env_file" not in api
    assert "model-egress" in worker["networks"]
    assert worker["env_file"]
    assert compose["services"]["postgres"]["networks"] == ["database"]
    assert "service-api" in api["networks"]
    assert "service-api" not in worker["networks"]


def test_exchange_volume_has_stable_external_name(compose):
    volume = compose["volumes"]["exchange-data"]
    assert volume["name"] == "${EXCHANGE_VOLUME_NAME:-hyper-extract-exchange}"
    assert compose["networks"]["service-api"]["name"] == "${API_NETWORK_NAME:-hyper-extract-api}"


def test_three_networks_have_distinct_responsibilities(compose):
    networks = compose["networks"]
    # database is internal-only (postgres + migrate + service cores).
    assert networks["database"]["internal"] is True
    # service-api is internal and named for external callers to attach to.
    assert networks["service-api"]["internal"] is True
    # model-egress has outbound access (no `internal: true`).
    assert "internal" not in networks["model-egress"] or networks["model-egress"].get("internal") is not True


def test_api_and_worker_share_the_same_profile_mount(compose):
    api = compose["services"]["he-api"]
    worker = compose["services"]["he-worker"]
    expected_env = "/run/config/model-profiles.toml"
    assert api["environment"]["HE_SERVICE_MODEL_PROFILES"] == expected_env
    assert worker["environment"]["HE_SERVICE_MODEL_PROFILES"] == expected_env
    expected_mount = "${MODEL_PROFILES_FILE:-./model-profiles.example.toml}:/run/config/model-profiles.toml:ro"
    assert expected_mount in api["volumes"]
    assert expected_mount in worker["volumes"]


def test_base_compose_publishes_no_api_port(compose):
    assert "ports" not in compose["services"]["he-api"]


def test_dev_override_binds_loopback_only_and_keeps_api_off_egress(dev_compose):
    api = dev_compose["services"]["he-api"]
    ports = api["ports"]
    assert len(ports) == 1
    assert ports[0].startswith("127.0.0.1:")
    assert "${HE_API_PORT:-8000}:8000" in ports[0]
    # The development override must NOT attach the API to the egress network.
    assert "model-egress" not in api.get("networks", [])


def test_env_example_lists_required_operator_variables():
    text = (ROOT / "docker" / ".env.example").read_text()
    for key in (
        "POSTGRES_PASSWORD",
        "EXCHANGE_VOLUME_NAME",
        "API_NETWORK_NAME",
        "HE_API_PORT",
        "PLATFORM",
        "HE_IMAGE",
        "MODEL_PROFILES_FILE",
    ):
        assert key in text


def test_docker_readme_documents_exchange_and_scaling():
    text = (ROOT / "docker" / "README.md").read_text()
    assert "/exchange" in text
    assert "10001" in text
    assert "--scale he-worker" in text
