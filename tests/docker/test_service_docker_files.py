"""Static tests for the hardened service Docker image and build context.

These tests read the Dockerfile and .dockerignore as text and assert on
content. They do NOT build the image, so they can run in CI without Docker.
"""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def test_docker_layout_uses_compose_conf_and_image_directories():
    assert (ROOT / "docker/compose.yml").is_file()
    assert (ROOT / "docker/compose.dev.yml").is_file()
    assert (ROOT / "docker/conf/model-profiles.example.toml").is_file()
    assert (ROOT / "docker/image/Dockerfile").is_file()
    assert (ROOT / "docker/image/entrypoint.sh").is_file()
    for old_path in (
        "docker/service.compose.yml",
        "docker/service.compose.dev.yml",
        "docker/service.Dockerfile",
        "docker/entrypoint.sh",
        "docker/model-profiles.example.toml",
    ):
        assert not (ROOT / old_path).exists()


def test_image_is_lockfile_based_and_non_root():
    text = (ROOT / "docker/image/Dockerfile").read_text()
    assert "ghcr.io/astral-sh/uv:" in text
    assert "uv sync --frozen" in text
    assert "USER 10001:10001" in text
    assert "pip install" not in text
    uv_image = re.search(r"FROM ghcr\.io/astral-sh/uv:([^ ]+)", text)
    assert uv_image is not None
    assert re.match(r"\d+\.\d+\.\d+-python3\.11-bookworm-slim$", uv_image.group(1))
    assert "FROM python:3.11-slim " not in text


def test_entrypoint_sets_group_writable_umask():
    text = (ROOT / "docker/image/entrypoint.sh").read_text()
    assert "umask 0002" in text
    assert text.index("umask 0002") < text.rindex('exec "$@"')


def test_context_excludes_secrets_and_runtime_data():
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert ".env" in ignored
    assert ".git/" in ignored
    assert "exchange/" in ignored


def test_docker_data_root_is_tracked_but_runtime_data_is_ignored():
    text = (ROOT / "docker/data/.gitignore").read_text().splitlines()
    assert text == ["*", "!.gitignore"]


def test_compose_uses_host_data_directories_and_no_named_volumes(compose):
    services = compose["services"]
    assert "he-migrate" not in services
    assert "volumes" not in compose
    postgres = services["postgres"]
    assert postgres["environment"]["PGDATA"] == "/var/lib/postgresql/data/pgdata"
    assert (
        "${HE_DATA_ROOT:-./data}/postgres:/var/lib/postgresql/data"
        in postgres["volumes"]
    )
    exchange = "${HE_DATA_ROOT:-./data}/exchange:/exchange"
    assert exchange in services["he-api"]["volumes"]
    assert exchange in services["he-worker"]["volumes"]
    assert (
        postgres["environment"]["POSTGRES_PASSWORD"]
        == "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}"
    )


def test_api_and_worker_depend_only_on_healthy_postgres(compose):
    services = compose["services"]
    for name in ("he-api", "he-worker"):
        assert services[name]["depends_on"] == {
            "postgres": {"condition": "service_healthy"}
        }
        assert services[name]["image"] == "${HE_IMAGE:-hyper-extract-service:dev}"
    assert "ports" not in services["postgres"]


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
    assert "${HE_DATA_ROOT:-./data}/exchange:/exchange" in worker["volumes"]


def test_service_api_network_has_stable_name(compose):
    assert (
        compose["networks"]["service-api"]["name"]
        == "${API_NETWORK_NAME:-hyper-extract-api}"
    )


def test_three_networks_have_distinct_responsibilities(compose):
    networks = compose["networks"]
    # database is internal-only (postgres + service cores).
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
    expected_mount = {
        "type": "bind",
        "source": "${MODEL_PROFILES_FILE:-./conf/model-profiles.example.toml}",
        "target": expected_env,
        "read_only": True,
        "bind": {"create_host_path": False},
    }
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
    assert "local-access" in api["networks"]
    assert dev_compose["networks"]["local-access"] == {}
    # The development override must NOT attach the API to the egress network.
    assert "model-egress" not in api.get("networks", [])


def test_env_example_lists_required_operator_variables():
    text = (ROOT / "docker" / ".env.example").read_text()
    for key in (
        "POSTGRES_PASSWORD",
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
    assert "EXCHANGE_VOLUME_NAME" not in text
    assert "MIMIMAX_API_KEY" not in text


def test_docker_readme_is_chinese_and_documents_configuration_boundaries():
    text = (ROOT / "docker" / "README.md").read_text()
    for required in (
        "# Docker 部署",
        "docker/compose.yml",
        "docker/compose.dev.yml",
        "MINIMAX_API_KEY",
        "model-profiles.example.toml",
        "只有 Worker",
        "/exchange",
        "10001",
        "不要使用 Compose `--scale`",
    ):
        assert required in text
    assert "service.compose" not in text
    assert "service.Dockerfile" not in text
    assert "MIMIMAX_API_KEY" not in text


def test_deploy_script_owns_pull_build_migrate_and_readiness():
    text = (ROOT / "scripts/deploy.sh").read_text()
    for required in (
        "set -Eeuo pipefail",
        "git pull --ff-only",
        "docker/data/postgres",
        "docker/data/exchange",
        "config --quiet",
        "build he-api",
        "up -d postgres",
        "pg_isready",
        "stop -t 20 he-api",
        "stop -t 90 he-worker",
        "run --rm --no-deps he-api alembic upgrade head",
        "up -d --remove-orphans he-api he-worker",
        "/health/ready",
        "HE_DEPLOY_REEXEC",
    ):
        assert required in text
    assert text.index("build he-api") < text.index("stop -t 20 he-api")
    assert text.index("stop -t 90 he-worker") < text.index(
        "alembic upgrade head"
    )
    assert "git reset" not in text
    assert "alembic downgrade" not in text


def test_deploy_script_reports_failure_diagnostics():
    text = (ROOT / "scripts/deploy.sh").read_text()
    assert "trap 'on_error" in text
    assert "compose ps" in text
    assert "logs --tail" in text


def test_smoke_script_uses_temporary_bind_mount_and_explicit_migration():
    text = (ROOT / "scripts" / "service-compose-smoke.sh").read_text()
    for required in (
        "mktemp -d",
        "HE_DATA_ROOT",
        "run --rm --no-deps he-api alembic upgrade head",
        "up -d postgres",
        "up -d he-api he-worker",
        "down --remove-orphans",
        "before_worker_id",
        "after_worker_id",
    ):
        assert required in text
    assert "EXCHANGE_VOLUME_NAME" not in text
    assert "he-migrate" not in text
    assert "down --volumes" not in text
    assert 'case "$SMOKE_DATA_ROOT"' in text


def test_api_acceptance_script_runs_explicit_migration_without_migrate_service():
    text = (ROOT / "scripts/service-api-course-test.sh").read_text()
    assert "run --rm --no-deps he-api alembic upgrade head" in text
    assert "pg_isready" in text
    assert "he-migrate" not in text
    assert 'volumes["exchange-data"]' not in text


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
