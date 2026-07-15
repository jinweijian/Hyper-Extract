"""Static tests for the hardened service Docker image and build context.

These tests read the Dockerfile and .dockerignore as text and assert on
content. They do NOT build the image, so they can run in CI without Docker.
"""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def test_image_is_lockfile_based_and_non_root():
    text = (ROOT / "docker/service.Dockerfile").read_text()
    assert "ghcr.io/astral-sh/uv:" in text
    assert "uv sync --frozen" in text
    assert "USER 10001:10001" in text
    assert "pip install" not in text
    uv_image = re.search(r"FROM ghcr\.io/astral-sh/uv:([^ ]+)", text)
    assert uv_image is not None
    assert re.match(r"\d+\.\d+\.\d+-python3\.11-bookworm-slim$", uv_image.group(1))
    assert "FROM python:3.11-slim " not in text


def test_entrypoint_sets_group_writable_umask():
    text = (ROOT / "docker/entrypoint.sh").read_text()
    assert "umask 0002" in text
    assert text.index("umask 0002") < text.rindex('exec "$@"')


def test_context_excludes_secrets_and_runtime_data():
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert ".env" in ignored
    assert ".git/" in ignored
    assert "exchange/" in ignored


def test_compose_has_migration_gate_and_database_volume(compose):
    services = compose["services"]
    assert services["he-migrate"]["command"] == [
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
    # `alembic upgrade head` chaining, since he-migrate owns migrations. The
    # runtime image places the venv on PATH, so the binaries run directly
    # without `uv run`.
    assert services["he-api"]["command"] == ["he-api"]
    assert services["he-worker"]["command"] == ["he-worker"]
    # PostgreSQL must never publish ports to the host.
    assert "ports" not in services["postgres"]
    # postgres-data volume must be declared at the top level.
    assert "postgres-data" in compose["volumes"]
    for name in ("he-migrate", "he-api", "he-worker"):
        assert services[name]["image"] == "${HE_IMAGE:-hyper-extract-service:dev}"


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
    for secret in ("OPENAI_API_KEY", "EMBEDDING_API_KEY", "ANTHROPIC_API_KEY"):
        assert secret not in api["environment"]
    for setting in (
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL",
    ):
        assert setting in api["environment"]


def test_worker_persists_capability_probe_evidence_in_exchange(compose):
    worker = compose["services"]["he-worker"]
    assert worker["environment"]["HE_PROBE_ROOT"] == "/exchange/probes"
    assert "exchange-data:/exchange" in worker["volumes"]


def test_exchange_volume_has_stable_external_name(compose):
    volume = compose["volumes"]["exchange-data"]
    assert volume["name"] == "${EXCHANGE_VOLUME_NAME:-hyper-extract-exchange}"
    assert (
        compose["networks"]["service-api"]["name"]
        == "${API_NETWORK_NAME:-hyper-extract-api}"
    )


def test_three_networks_have_distinct_responsibilities(compose):
    networks = compose["networks"]
    # database is internal-only (postgres + migrate + service cores).
    assert networks["database"]["internal"] is True
    # service-api is internal and named for external callers to attach to.
    assert networks["service-api"]["internal"] is True
    # model-egress has outbound access (no `internal: true`).
    assert (
        "internal" not in networks["model-egress"]
        or networks["model-egress"].get("internal") is not True
    )


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
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "EMBEDDING_MODEL",
        "EMBEDDING_API_KEY",
        "MINIMAX_API_KEY",
    ):
        assert key in text
    assert "MIMIMAX_API_KEY" not in text


def test_docker_readme_documents_exchange_and_safe_worker_limit():
    text = (ROOT / "docker" / "README.md").read_text()
    assert "/exchange" in text
    assert "10001" in text
    assert "do not use Compose `--scale`" in text


def test_smoke_script_is_isolated_and_cleans_up():
    text = (ROOT / "scripts" / "service-compose-smoke.sh").read_text()
    assert "set -eu" in text
    assert "OPENAI_API_KEY=" in text
    assert "--project-name" in text
    assert "down --volumes --remove-orphans" in text
    assert "trap " in text
    assert "before_worker_id" in text
    assert "after_worker_id" in text
    assert 'after_worker_id" != "$before_worker_id' in text


def test_api_uses_readiness_and_worker_has_shutdown_window(compose):
    api = compose["services"]["he-api"]
    worker = compose["services"]["he-worker"]
    assert "/health/ready" in " ".join(api["healthcheck"]["test"])
    assert worker["stop_grace_period"] == "90s"
    assert "container_name" not in worker


def test_api_healthcheck_timing_and_grace(compose):
    api = compose["services"]["he-api"]
    hc = api["healthcheck"]
    assert hc["interval"] == "10s"
    assert hc["timeout"] == "3s"
    assert hc["start_period"] == "20s"
    assert api["stop_grace_period"] == "20s"


def test_worker_has_no_http_healthcheck(compose):
    worker = compose["services"]["he-worker"]
    # A Worker must not carry a misleading Docker HTTP healthcheck: long-running
    # model calls would otherwise be killed by the orchestrator.
    assert "healthcheck" not in worker


def test_api_and_worker_restart_unless_stopped(compose):
    services = compose["services"]
    assert services["he-api"]["restart"] == "unless-stopped"
    assert services["he-worker"]["restart"] == "unless-stopped"
    # Migration remains one-shot.
    assert services["he-migrate"]["restart"] == "no"
