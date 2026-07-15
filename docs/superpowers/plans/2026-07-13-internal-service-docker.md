# Hyper-Extract Internal Service Docker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成可迁移、可持久化、可共享数据卷、可健康检查并可安全扩展 Worker 的 Hyper-Extract 内部 Docker 部署方案。

**Architecture:** 一个锁文件构建的非 root 镜像承担 migrate、API 和 Worker 三种角色。Compose 使用独立 PostgreSQL 数据卷和显式命名的 `/exchange` 卷；API 只接控制网络和无密钥 Profile，Worker 额外接 egress 网络并独占模型密钥。

**Tech Stack:** Docker Engine、Docker Compose v2、linux/amd64、Python 3.11、uv、PostgreSQL、Hyper-Extract `he-api`/`he-worker`。

---

**Dependency:** Complete [Internal API Service Implementation Plan](./2026-07-13-internal-api-service.md) through Task 6 before final Compose verification.

### Task 1: Harden the Service Image and Build Context

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/docker/service.Dockerfile`
- Create: `/Users/king/website/Hyper-Extract/docker/entrypoint.sh`
- Create: `/Users/king/website/Hyper-Extract/.dockerignore`
- Create: `/Users/king/website/Hyper-Extract/tests/docker/test_service_docker_files.py`

- [ ] **Step 1: Write failing static image tests**

```python
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
```

- [ ] **Step 2: Verify RED**

```bash
OPENAI_API_KEY="" uv run pytest tests/docker/test_service_docker_files.py -q
```

- [ ] **Step 3: Implement the pinned multi-stage image**

Use a pinned `ghcr.io/astral-sh/uv:<version>` stage, `python:3.11-slim` runtime, `uv sync --frozen --no-dev --extra service --extra graph-rag`, UID/GID `10001`, and `ENTRYPOINT ["/usr/local/bin/he-entrypoint"]`. The entrypoint must only validate `/exchange` readability and end with `exec "$@"`; it must not migrate the database or recursively chown volumes.

- [ ] **Step 4: Build and inspect**

```bash
docker build --platform linux/amd64 -f docker/service.Dockerfile -t hyper-extract-service:dev .
docker run --rm --entrypoint id hyper-extract-service:dev
docker run --rm --entrypoint sh hyper-extract-service:dev -c 'test "$(id -u)" = 10001'
```

- [ ] **Step 5: Verify and commit**

```bash
OPENAI_API_KEY="" uv run pytest tests/docker/test_service_docker_files.py -q
git add .dockerignore docker/service.Dockerfile docker/entrypoint.sh tests/docker/test_service_docker_files.py
git commit -m "build: harden service image"
```

### Task 2: Separate Migration and Persist PostgreSQL

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/db.py`
- Modify: `/Users/king/website/Hyper-Extract/docker/service.compose.yml`
- Modify: `/Users/king/website/Hyper-Extract/tests/docker/test_service_docker_files.py`

- [ ] **Step 1: Write failing topology tests**

```python
def test_compose_has_migration_gate_and_database_volume(compose):
    services = compose["services"]
    assert services["he-migrate"]["command"] == ["uv", "run", "--no-sync", "alembic", "upgrade", "head"]
    assert services["he-api"]["depends_on"]["he-migrate"]["condition"] == "service_completed_successfully"
    assert services["he-worker"]["depends_on"]["he-migrate"]["condition"] == "service_completed_successfully"
    assert "postgres-data:/var/lib/postgresql/data" in services["postgres"]["volumes"]
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD"] == "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}"
```

- [ ] **Step 2: Remove runtime `create_all()` from production startup**

Change `create_engine_and_session()` so `Base.metadata.create_all(engine)` runs only when `for_tests=True`. Production schema ownership belongs exclusively to Alembic. Update test fixtures to pass `for_tests=True`.

- [ ] **Step 3: Add one-shot `he-migrate`**

The Compose dependency chain is:

```text
postgres healthy -> he-migrate exits 0 -> he-api + he-worker start
```

`he-migrate` uses `restart: "no"`; API command is only `he-api`; Worker command is only `he-worker`. Add `postgres-data:/var/lib/postgresql/data`, read the database password from `${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}`, and never publish PostgreSQL to the host.

- [ ] **Step 4: Validate and commit**

```bash
docker compose -f docker/service.compose.yml config --quiet
OPENAI_API_KEY="" uv run pytest tests/service/test_repository.py tests/docker/test_service_docker_files.py -q
git add hyperextract/service/db.py tests/service docker/service.compose.yml tests/docker
git commit -m "build: gate service startup on migrations"
```

### Task 3: Establish the Shared-volume and Secret Boundary

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/docker/service.compose.yml`
- Modify: `/Users/king/website/Hyper-Extract/docker/service.compose.dev.yml`
- Create: `/Users/king/website/Hyper-Extract/docker/.env.example`
- Modify: `/Users/king/website/Hyper-Extract/docker/model-profiles.example.toml`
- Create: `/Users/king/website/Hyper-Extract/docker/README.md`

- [ ] **Step 1: Write failing network and volume tests**

```python
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
```

- [ ] **Step 2: Mount the same Profile file in API and Worker**

Both services receive:

```yaml
environment:
  HE_SERVICE_MODEL_PROFILES: /run/config/model-profiles.toml
volumes:
  - ${MODEL_PROFILES_FILE:-./model-profiles.example.toml}:/run/config/model-profiles.toml:ro
```

Only Worker has `env_file: .env` and the `model-egress` network. API must be able to calculate the secret-free Profile fingerprint without provider keys.

- [ ] **Step 3: Give `/exchange` a stable name and path**

```yaml
volumes:
  exchange-data:
    name: ${EXCHANGE_VOLUME_NAME:-hyper-extract-exchange}
```

API, Worker and caller mount it as `/exchange`. Document caller-side external volume configuration and atomic `.staging-*` to final-directory rename. Do not use `chmod 777`; document UID/GID `10001` and `umask 0002`.

Use three networks with distinct responsibilities:

```yaml
networks:
  database:
    internal: true
  service-api:
    name: ${API_NETWORK_NAME:-hyper-extract-api}
    internal: true
  model-egress: {}
```

PostgreSQL and migrate join only `database`; API joins `database` and `service-api`; Worker joins `database` and `model-egress`. Calling services declare `hyper-extract-api` as an external network and cannot reach PostgreSQL directly.

- [ ] **Step 4: Restrict port exposure**

Base Compose publishes no API port. Development override binds only:

```yaml
services:
  he-api:
    ports:
      - "127.0.0.1:${HE_API_PORT:-8000}:8000"
```

Do not attach API to egress in the development override. `docker/.env.example` includes `POSTGRES_PASSWORD`, `EXCHANGE_VOLUME_NAME`, `API_NETWORK_NAME`, `HE_API_PORT`, `PLATFORM`, `HE_IMAGE` and `MODEL_PROFILES_FILE`, using placeholders rather than real secrets.

- [ ] **Step 5: Validate and commit**

```bash
docker compose --env-file docker/.env.example -f docker/service.compose.yml -f docker/service.compose.dev.yml config --quiet
OPENAI_API_KEY="" uv run pytest tests/docker/test_service_docker_files.py -q
git add docker tests/docker
git commit -m "build: define service volume and secret boundaries"
```

### Task 4: Add Health, Shutdown, and Scaling Policy

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/docker/service.compose.yml`
- Modify: `/Users/king/website/Hyper-Extract/docker/README.md`
- Modify: `/Users/king/website/Hyper-Extract/tests/docker/test_service_docker_files.py`

- [ ] **Step 1: Add failing health-policy tests**

```python
def test_api_uses_readiness_and_worker_has_shutdown_window(compose):
    api = compose["services"]["he-api"]
    worker = compose["services"]["he-worker"]
    assert "/health/ready" in " ".join(api["healthcheck"]["test"])
    assert worker["stop_grace_period"] == "90s"
    assert "container_name" not in worker
```

- [ ] **Step 2: Add health and restart policy**

- PostgreSQL: `pg_isready`.
- API: `/health/ready`, 10-second interval, 3-second timeout, start period 20 seconds.
- API stop grace: 20 seconds.
- Worker stop grace: 90 seconds; no misleading Docker HTTP healthcheck.
- API/Worker restart: `unless-stopped`.
- Migration restart: `"no"`.

- [ ] **Step 3: Document scaling and recovery**

```bash
docker compose --env-file docker/.env -f docker/service.compose.yml up -d --scale he-worker=3
```

Explain that replicas share PostgreSQL and `/exchange`, claim with `SKIP LOCKED`, renew leases, and resume checkpoints after expiry. Explicitly prohibit `container_name`.

- [ ] **Step 4: Validate and commit**

```bash
docker compose -f docker/service.compose.yml config --quiet
OPENAI_API_KEY="" uv run pytest tests/docker/test_service_docker_files.py -q
git add docker tests/docker
git commit -m "build: add service health and scaling policy"
```

### Task 5: Add a Deterministic Compose Smoke Test

**Files:**
- Create: `/Users/king/website/Hyper-Extract/scripts/service-compose-smoke.sh`
- Modify: `/Users/king/website/Hyper-Extract/docker/README.md`
- Modify: `/Users/king/website/Hyper-Extract/tests/docker/test_service_docker_files.py`

- [ ] **Step 1: Write the failing script contract test**

```python
def test_smoke_script_is_isolated_and_cleans_up():
    text = (ROOT / "scripts/service-compose-smoke.sh").read_text()
    assert "set -eu" in text
    assert "OPENAI_API_KEY=" in text
    assert "--project-name" in text
    assert "down --volumes --remove-orphans" in text
    assert "trap " in text
```

- [ ] **Step 2: Implement isolated smoke behavior**

The script uses a unique project name and exchange volume, sets provider keys empty, registers cleanup, builds the image, starts PostgreSQL/migration/API/Worker, waits for readiness, verifies API and a helper container see the same `/exchange` sentinel, restarts API/Worker, and verifies the sentinel again. It must not submit a real extraction run.

The Docker README must distinguish smoke-test cleanup from operator data handling. Document PostgreSQL backup, `/exchange/runs` backup when cross-host resume is required, a retention policy for `.he-run` Brief/Prompt snapshots, and an explicit warning that `docker compose down --volumes` destroys database and run state and is only used by the isolated smoke project.

- [ ] **Step 3: Add an opt-in real-provider acceptance runbook**

Document a separate manual procedure: inject Worker keys, publish one small immutable Package 1.1 with Brief, validate, create, poll, verify `_SUCCESS` plus every manifest hash, then inspect `run-summary.json.extraction_brief`. Never make this part of deterministic CI.

- [ ] **Step 4: Run the production Docker gate**

```bash
OPENAI_API_KEY="" uv run pytest tests/docker tests/service -q
uv run ruff check hyperextract
uv run ruff format --check hyperextract
docker compose --env-file docker/.env.example -f docker/service.compose.yml -f docker/service.compose.dev.yml config --quiet
sh scripts/service-compose-smoke.sh
```

- [ ] **Step 5: Commit**

```bash
git add scripts/service-compose-smoke.sh docker/README.md tests/docker/test_service_docker_files.py
git commit -m "test: add service compose smoke coverage"
```

## Docker Completion Gate

- Image is deterministic, non-root and contains API, Worker and migration commands.
- PostgreSQL has a persistent volume and is never host-published.
- Migration is a one-shot gate; runtime code does not create production tables.
- API has no model keys or egress; Worker has both only as needed; callers cannot reach PostgreSQL through the API network.
- Caller/API/Worker share the same named volume at `/exchange`.
- API readiness is a Compose healthcheck; Worker leases are visible in PostgreSQL.
- `--scale he-worker=N` works without duplicate task claims.
- Smoke test uses no real provider and removes only its own resources.
