# Hyper-Extract Internal API Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 Hyper-Extract 内部异步 API 的契约、任务生命周期、恢复、失败诊断和共享卷产物实现，使 Document Package 1.1 与 ExtractionBrief 达到可部署状态。

**Architecture:** `hyperextract.service` 是 API 与 Worker 共用的服务边界，`hyperextract.service.api` 是独立 HTTP 适配层。API 目录只负责路由、依赖注入和请求/响应 Schema；Repository、数据库、共享卷、产物与 Model Profile 留在 `service` 根层供 API 和 Worker 复用。Worker 独占模型密钥并通过租约执行任务，文件系统保存 checkpoint 与原子发布产物。

**Tech Stack:** Python 3.11/3.12、FastAPI、Pydantic v2、SQLAlchemy 2.x、PostgreSQL、Alembic、pytest、现有 Course Pipeline 与 ExtractionBrief。

---

## Scope

- Contract source of truth: `/Users/king/website/Hyper-Extract/docs/zh/guides/internal-service.md`.
- Production package version: Document Package `1.1`; Package `1.0` remains readable for compatibility.
- Brief Schema remains `HyperExtractExtractionBrief@1.0` and stays inside the Package.
- No authentication, SSE, Webhook, upload API, object storage, queue service, task listing or tenant model.
- Keep `context_policy`, `priority` and `budget` as explicitly documented reserved fields in this iteration; do not pretend they affect execution.
- Keep `community_reports=false` and the unconnected `evaluation` stage as documented limitations.
- Every deterministic test command must set `OPENAI_API_KEY=""`.

## Target File Changes

```text
hyperextract/service/
  runtime.py             shared API/Worker runtime composition
  api/
    __init__.py
    app.py                FastAPI application factory only
    main.py               he-api command entry point
    dependencies.py       request-to-runtime dependency injection
    routes/
      __init__.py
      health.py           liveness and readiness
      contracts.py        capabilities and Package contract endpoints
      runs.py             create, poll, cancel, resume, errors, artifacts
    schemas/
      __init__.py
      requests.py         strict HTTP request models
      responses.py        stable public response models
  commands.py             RunCommand shared by API and repository
  contracts.py            service-layout validation and contract discovery
  model_profiles.py       split public fingerprint from secret resolution
  db_models.py            attempts, errors and Worker heartbeats
  repository.py           concurrency, lifecycle, leases and diagnostics
  artifacts.py            published-artifact reconciliation
  worker.py               heartbeat, recovery, cancellation and redaction
  migrations/versions/
    0002_service_recovery.py
tests/service/
  test_api_structure.py
  test_contracts_api.py
  test_model_profiles.py
  test_repository.py
  test_repository_postgres.py
  test_runs_api.py
  test_worker.py
  test_artifacts.py
  test_readiness.py
docs/zh/guides/internal-service.md
docs/en/guides/internal-service.md
```

### Task 1: Establish the `service/api` Package Boundary

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/pyproject.toml`
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/commands.py`
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/runtime.py`
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/api/__init__.py`
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/api/app.py`
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/api/main.py`
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/api/dependencies.py`
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/api/routes/__init__.py`
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/api/routes/health.py`
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/api/routes/contracts.py`
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/api/routes/runs.py`
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/api/schemas/__init__.py`
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/api/schemas/requests.py`
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/api/schemas/responses.py`
- Delete: `/Users/king/website/Hyper-Extract/hyperextract/service/app.py`
- Delete: `/Users/king/website/Hyper-Extract/hyperextract/service/schemas.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/repository.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/worker.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/conftest.py`
- Create: `/Users/king/website/Hyper-Extract/tests/service/test_api_structure.py`

- [ ] **Step 1: Write failing package-boundary tests**

```python
import pytest
from fastapi.testclient import TestClient

from hyperextract.service.api.app import create_app
from hyperextract.service.commands import RunCommand
from hyperextract.service.runtime import create_runtime


@pytest.fixture
def fake_profiles():
    class FakeProfiles:
        def public_descriptor(self, name):
            if name != "minimax-course-default":
                raise KeyError(name)
            return {"name": name, "fingerprint": "b" * 64}

    return FakeProfiles()


def test_api_routes_are_registered(settings, repository, fake_profiles):
    runtime = create_runtime(
        settings=settings,
        repository=repository,
        model_profiles=fake_profiles,
    )
    with TestClient(
        create_app(runtime=runtime)
    ) as client:
        paths = {route.path for route in client.app.routes}
    assert "/health/live" in paths
    assert "/v1/contracts/document-package/v1" in paths
    assert "/v1/runs" in paths


def test_run_command_is_not_owned_by_http_schemas():
    command = RunCommand(
        run_id="run_test",
        request_fingerprint="a" * 64,
        request_json={"input": {}},
        output_uri="file:///exchange/runs/run_test/",
    )
    assert command.run_id == "run_test"
```

- [ ] **Step 2: Verify RED**

```bash
cd /Users/king/website/Hyper-Extract
OPENAI_API_KEY="" uv run pytest tests/service/test_api_structure.py -q
```

Expected: imports fail because `hyperextract.service.api`, `service.commands` and `service.runtime` do not exist.

- [ ] **Step 3: Move the internal command model out of HTTP schemas**

Create `service/commands.py`:

```python
from pydantic import BaseModel, ConfigDict, Field


class RunCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_json: dict[str, object]
    output_uri: str
```

Update Repository, tests and Worker imports from `service.schemas.RunCommand` to `service.commands.RunCommand`.

- [ ] **Step 4: Split strict HTTP request models**

Move `StrictModel`, `RunInput`, `RunBudget`, `RunExecution`, `ProfileSelection`, `PipelineSelection`, `ClientContext`, `RunCreateRequest` and `ValidatePackageRequest` into `service/api/schemas/requests.py`. Export only `RunCreateRequest` and `ValidatePackageRequest` from `service/api/schemas/__init__.py`.

Create public response primitives in `responses.py`:

```python
from pydantic import BaseModel, ConfigDict, Field


class PublicResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class OutputResponse(PublicResponse):
    run_uri: str
    artifacts_uri: str
    manifest_uri: str
    success_marker_uri: str


class RunLinksResponse(PublicResponse):
    self_url: str = Field(alias="self")
    cancel: str
    resume: str
    errors: str
    artifacts: str


class RunResponse(PublicResponse):
    run_id: str
    status: str
    stage: str
    stage_status: str
    attempt: int
    progress: dict[str, object]
    error_summary: dict[str, object] | None
    resumable: bool
    cancel_requested: bool
    output: OutputResponse
    links: RunLinksResponse
```

- [ ] **Step 5: Create the shared service runtime**

`service/runtime.py` is the only place that composes shared infrastructure:

```python
from dataclasses import dataclass

from sqlalchemy.engine import Engine

from hyperextract.service.db import create_engine_and_session
from hyperextract.service.model_profiles import ModelProfileRegistry
from hyperextract.service.repository import RunRepository
from hyperextract.service.settings import ServiceSettings
from hyperextract.service.storage import SharedVolumeStore


@dataclass
class ServiceRuntime:
    settings: ServiceSettings
    repository: RunRepository
    storage: SharedVolumeStore
    model_profiles: ModelProfileRegistry
    owned_engine: Engine | None = None

    def prepare(self) -> None:
        self.settings.package_root.mkdir(parents=True, exist_ok=True)
        self.settings.run_root.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        if self.owned_engine is not None:
            self.owned_engine.dispose()


def create_runtime(
    settings: ServiceSettings | None = None,
    repository: RunRepository | None = None,
    model_profiles: ModelProfileRegistry | None = None,
) -> ServiceRuntime:
    resolved = settings or ServiceSettings.from_env()
    owned_engine = None
    if repository is None:
        owned_engine, session_factory = create_engine_and_session(
            resolved.database_url
        )
        repository = RunRepository(session_factory)
    return ServiceRuntime(
        settings=resolved,
        repository=repository,
        storage=SharedVolumeStore(resolved.exchange_root),
        model_profiles=model_profiles
        or ModelProfileRegistry(resolved.model_profiles_path),
        owned_engine=owned_engine,
    )
```

Runtime construction must not import FastAPI or any module under `service.api`.

- [ ] **Step 6: Move dependency injection and route ownership**

`api/dependencies.py` only retrieves the already-built runtime from the HTTP request:

```python
from fastapi import Request

from hyperextract.service.runtime import ServiceRuntime


def get_runtime(request: Request) -> ServiceRuntime:
    return request.app.state.runtime
```

Move endpoints without changing behavior according to this ownership map:

```text
routes/health.py:     /health/live, /health/ready
routes/contracts.py:  /v1/capabilities, /v1/contracts/document-package/v1,
                      /v1/document-packages/validate
routes/runs.py:       /v1/runs and every /v1/runs/{run_id} endpoint
```

Each route module exports one `APIRouter`. Route functions may depend on service-core modules, but service-core modules must never import `service.api`.
Declare `response_model=RunResponse` on create, get, cancel and resume routes so the public response contract is generated from `api/schemas/responses.py` rather than an untyped dictionary.

- [ ] **Step 7: Build the thin FastAPI factory**

`service/api/app.py` creates only the FastAPI application and binds an existing or default runtime:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from hyperextract.service.errors import ServiceError
from hyperextract.service.runtime import ServiceRuntime, create_runtime

from .routes import contracts, health, runs


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def service_error_handler(_request, error: ServiceError):
        return JSONResponse(
            status_code=error.status_code,
            content=error.body(),
        )


def create_app(runtime: ServiceRuntime | None = None) -> FastAPI:
    resolved_runtime = runtime or create_runtime()
    owns_runtime = runtime is None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        resolved_runtime.prepare()
        yield
        if owns_runtime:
            resolved_runtime.close()

    app = FastAPI(
        title="Hyper-Extract Internal Service",
        version="1.0",
        lifespan=lifespan,
    )
    app.state.runtime = resolved_runtime
    app.include_router(health.router)
    app.include_router(contracts.router)
    app.include_router(runs.router)
    register_exception_handlers(app)
    return app
```

Do not construct Repository, Storage or Model Profile objects in route modules or `api/app.py`.

- [ ] **Step 8: Create the API command bootstrap**

`service/api/main.py` owns only the terminal-to-Uvicorn bridge:

```python
def main() -> None:
    import uvicorn

    uvicorn.run(
        "hyperextract.service.api.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
    )
```

Update the executable entry point:

```toml
[project.scripts]
he-api = "hyperextract.service.api.main:main"
```

Update `tests/service/conftest.py` to import `create_app` from the new path. Delete the old `service/app.py` and `service/schemas.py` only after `rg 'service\.(app|schemas)' hyperextract tests` returns no matches.

- [ ] **Step 9: Make Worker reuse the same runtime composition**

Replace manual settings/engine/repository construction in `worker.main()` with:

```python
import signal
import time

from hyperextract.service.runtime import create_runtime


def run_worker_loop(worker: ServiceWorker, settings: ServiceSettings) -> None:
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while not stopped:
        if not worker.run_once():
            time.sleep(settings.poll_seconds)


def main() -> None:
    runtime = create_runtime()
    runtime.prepare()
    worker = ServiceWorker(
        runtime.repository,
        CourseRunExecutor(
            runtime.settings,
            runtime.repository,
            runtime.model_profiles,
        ),
        ArtifactPublisher(runtime.settings.run_root),
        runtime.settings,
        worker_id="worker-" + uuid.uuid4().hex[:12],
    )
    try:
        run_worker_loop(worker, runtime.settings)
    finally:
        runtime.close()
```

Move the existing signal handling and polling loop into `run_worker_loop()` without changing behavior. Worker imports `service.runtime`; runtime never imports Worker.

- [ ] **Step 10: Verify and commit**

```bash
OPENAI_API_KEY="" uv run pytest tests/service -q
uv run ruff check hyperextract/service
git add pyproject.toml hyperextract/service tests/service
git commit -m "refactor(service): isolate api adapter"
```

Expected: all existing API and Worker behavior remains green; `api/app.py` contains no database, storage or Model Profile construction.

### Task 2: Align API Contract Version with Document Package 1.1

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/api/schemas/requests.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/contracts.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/api/routes/contracts.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/api/routes/runs.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_contracts_api.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_runs_api.py`

- [ ] **Step 1: Write failing version and layout tests**

```python
def test_validate_accepts_v1_1_only_when_manifest_matches(client, package_v1_1):
    response = client.post(
        "/v1/document-packages/validate",
        json={
            "contract_version": "1.1",
            "package_uri": package_v1_1.as_uri(),
            "sha256": document_package_fingerprint(package_v1_1),
        },
    )
    assert response.status_code == 200
    assert response.json()["schema_version"] == "1.1"


def test_validate_rejects_declared_version_mismatch(client, package_v1_1):
    response = client.post(
        "/v1/document-packages/validate",
        json={
            "contract_version": "1.0",
            "package_uri": package_v1_1.as_uri(),
            "sha256": document_package_fingerprint(package_v1_1),
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DOCUMENT_PACKAGE_VERSION_MISMATCH"


def test_service_contract_rejects_nonstandard_layout(client, package_v1_1):
    manifest = json.loads((package_v1_1 / "manifest.json").read_text())
    manifest["outline_path"] = "metadata/custom-outline.json"
    (package_v1_1 / "metadata").mkdir()
    (package_v1_1 / "outline.json").rename(
        package_v1_1 / "metadata/custom-outline.json"
    )
    (package_v1_1 / "manifest.json").write_text(json.dumps(manifest))

    response = validate_request(client, package_v1_1, version="1.1")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DOCUMENT_PACKAGE_LAYOUT_INVALID"
```

- [ ] **Step 2: Verify RED**

```bash
cd /Users/king/website/Hyper-Extract
OPENAI_API_KEY="" uv run pytest tests/service/test_contracts_api.py tests/service/test_runs_api.py -q
```

Expected: v1.1 request currently fails Pydantic validation and layout mismatch is accepted.

- [ ] **Step 3: Expand the request version type**

```python
DocumentPackageVersion = Literal["1.0", "1.1"]


class RunInput(StrictModel):
    type: Literal["document_package"]
    contract_version: DocumentPackageVersion
    package_uri: str
    package_format: Literal["directory"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ValidatePackageRequest(StrictModel):
    contract_version: DocumentPackageVersion
    package_uri: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
```

- [ ] **Step 4: Add a service-boundary Package validator**

Keep the generic core reader flexible; enforce the public service layout in `contracts.py`:

```python
from hyperextract.documents.document_package import ValidatedDocumentPackage


def validate_service_package_layout(
    package: ValidatedDocumentPackage,
    declared_version: str,
) -> None:
    manifest = package.manifest
    if manifest.schema_version != declared_version:
        raise ServicePackageContractError("DOCUMENT_PACKAGE_VERSION_MISMATCH")
    if manifest.outline_path != "outline.json":
        raise ServicePackageContractError("DOCUMENT_PACKAGE_LAYOUT_INVALID")
    if manifest.provenance_path != "provenance.jsonl":
        raise ServicePackageContractError("DOCUMENT_PACKAGE_LAYOUT_INVALID")
    if any(not item.path.startswith("content/") for item in manifest.contents):
        raise ServicePackageContractError("DOCUMENT_PACKAGE_LAYOUT_INVALID")
    if manifest.schema_version == "1.1":
        if manifest.extraction_brief is None:
            raise ServicePackageContractError("DOCUMENT_PACKAGE_LAYOUT_INVALID")
        if manifest.extraction_brief.path != "extraction-brief.yaml":
            raise ServicePackageContractError("DOCUMENT_PACKAGE_LAYOUT_INVALID")
```

Define `ServicePackageContractError` with a stable `code` property. Both validate and create routes must call `validate_document_package()` once, call this service validator, and then verify the canonical Package fingerprint.

- [ ] **Step 5: Reject ambiguous file URIs**

Extend `SharedVolumeStore.resolve_package_uri()` tests and implementation to reject `parsed.query` or `parsed.fragment` with `DOCUMENT_PACKAGE_URI_INVALID`. Do not silently ignore them.

- [ ] **Step 6: Verify GREEN and commit**

```bash
OPENAI_API_KEY="" uv run pytest tests/service/test_contracts_api.py tests/service/test_runs_api.py tests/service/test_storage.py -q
uv run ruff check hyperextract/service
git add hyperextract/service tests/service
git commit -m "fix(service): align package contract versions"
```

### Task 3: Make Model Profile Fingerprints Secret-free and Consistent

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/model_profiles.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/api/routes/runs.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/runner.py`
- Create: `/Users/king/website/Hyper-Extract/tests/service/test_model_profiles.py`

- [ ] **Step 1: Write failing secret-boundary tests**

```python
def test_public_descriptor_never_requires_or_contains_api_keys(profile_file, monkeypatch):
    monkeypatch.delenv("MIMIMAX_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    registry = ModelProfileRegistry(profile_file)

    descriptor = registry.public_descriptor("minimax-course-default")

    assert descriptor["name"] == "minimax-course-default"
    assert "api_key" not in json.dumps(descriptor).lower()
    assert len(descriptor["fingerprint"]) == 64


def test_runtime_resolution_requires_worker_secrets(profile_file, monkeypatch):
    monkeypatch.delenv("MIMIMAX_API_KEY", raising=False)
    registry = ModelProfileRegistry(profile_file)

    with pytest.raises(ValueError, match="MODEL_PROFILE_ENV_MISSING"):
        registry.resolve_runtime("minimax-course-default")
```

- [ ] **Step 2: Verify RED**

```bash
OPENAI_API_KEY="" uv run pytest tests/service/test_model_profiles.py -q
```

Expected: `public_descriptor()` currently calls `get()` and requires secret values.

- [ ] **Step 3: Split public specification from runtime resolution**

Implement these interfaces:

```python
@dataclass(frozen=True)
class ModelProfileSpec:
    name: str
    llm: str
    llm_api_key_env: str
    embedder: str
    embedder_api_key_env: str
    structured_output_mode: str = "text_json"
    output_repair_attempts: int = 1
    request_timeout: int = 900
    max_tokens: int | None = None


class ModelProfileRegistry:
    def get_spec(self, name: str) -> ModelProfileSpec:
        raw = self._raw_profiles().get(name)
        if not isinstance(raw, dict):
            raise KeyError(name)
        return ModelProfileSpec(
            name=name,
            llm=str(raw["llm"]),
            llm_api_key_env=str(raw["llm_api_key_env"]),
            embedder=str(raw["embedder"]),
            embedder_api_key_env=str(raw["embedder_api_key_env"]),
            structured_output_mode=str(
                raw.get("structured_output_mode", "text_json")
            ),
            output_repair_attempts=int(raw.get("output_repair_attempts", 1)),
            request_timeout=int(raw.get("request_timeout", 900)),
            max_tokens=int(raw["max_tokens"]) if raw.get("max_tokens") else None,
        )

    def public_descriptor(self, name: str) -> dict[str, object]:
        spec = self.get_spec(name)
        public = {
            "name": spec.name,
            "llm": spec.llm,
            "embedder": spec.embedder,
            "structured_output_mode": spec.structured_output_mode,
            "output_repair_attempts": spec.output_repair_attempts,
            "request_timeout": spec.request_timeout,
            "max_tokens": spec.max_tokens,
        }
        public["fingerprint"] = fingerprint(public)
        return public

    def resolve_runtime(self, name: str) -> ResolvedModelProfile:
        spec = self.get_spec(name)
        return ResolvedModelProfile(
            name=spec.name,
            llm=spec.llm,
            llm_api_key=self._required_env(spec.llm_api_key_env),
            embedder=spec.embedder,
            embedder_api_key=self._required_env(spec.embedder_api_key_env),
            structured_output_mode=spec.structured_output_mode,
            output_repair_attempts=spec.output_repair_attempts,
            request_timeout=spec.request_timeout,
            max_tokens=spec.max_tokens,
        )
```

For the service process, remove the implicit built-in fallback and require a readable `HE_SERVICE_MODEL_PROFILES` TOML file. The TOML contains concrete LLM/embedder addresses and the names of secret environment variables; only `resolve_runtime()` dereferences those secret variable names.

The fingerprint includes model addresses and behavioral options but never secret values. `CourseRunExecutor` uses `resolve_runtime()`; API creation uses `public_descriptor()`.

- [ ] **Step 4: Verify API creation without model secrets**

Add an API test that mounts the same Profile TOML used by Worker, removes all provider keys, and still receives `202`. Then run:

```bash
OPENAI_API_KEY="" uv run pytest tests/service/test_model_profiles.py tests/service/test_runs_api.py -q
uv run ruff check hyperextract/service/model_profiles.py hyperextract/service/api hyperextract/service/runner.py
git add hyperextract/service tests/service/test_model_profiles.py tests/service/test_runs_api.py
git commit -m "fix(service): separate model profile secrets"
```

### Task 4: Persist Attempts, Error History, and Worker Heartbeats

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/db_models.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/repository.py`
- Create: `/Users/king/website/Hyper-Extract/hyperextract/service/migrations/versions/0002_service_recovery.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/api/routes/runs.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/api/schemas/responses.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_repository.py`
- Create: `/Users/king/website/Hyper-Extract/tests/service/test_repository_postgres.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_runs_api.py`

- [ ] **Step 1: Write failing repository and API tests**

```python
def test_failure_is_recorded_and_queryable(repository, running_run):
    repository.fail(
        running_run.run_id,
        code="MODEL_RATE_LIMIT_EXHAUSTED",
        message="Provider request failed after retries",
        resumable=True,
    )
    errors = repository.list_errors(running_run.run_id)
    assert errors[0].code == "MODEL_RATE_LIMIT_EXHAUSTED"
    assert errors[0].attempt == 1


def test_errors_endpoint_returns_attempt_history(client, failed_run):
    response = client.get(f"/v1/runs/{failed_run.run_id}/errors")
    assert response.status_code == 200
    assert response.json()["errors"][0]["code"] == "RUN_EXECUTION_FAILED"
```

- [ ] **Step 2: Add migration `0002_service_recovery`**

Create:

```text
he_run_attempts(id, run_id, attempt, status, started_at, ended_at)
he_run_errors(error_id, run_id, attempt, code, source, message, details_json, occurred_at)
he_worker_heartbeats(worker_id, version, last_seen_at)
```

Add unique `(run_id, attempt)`, foreign keys to `he_runs`, and indexes on error lookup and heartbeat time. Add `artifacts_deleted_at` to `he_runs` only if retention is implemented in this iteration; otherwise do not add unused state.

- [ ] **Step 3: Make concurrent creation idempotent on PostgreSQL**

`create_or_get()` must catch the unique-key race and read the winner after rollback:

```python
try:
    with self.session_factory.begin() as session:
        session.add(new_row)
        session.flush()
except IntegrityError:
    with self.session_factory() as session:
        existing = session.scalar(
            select(RunEntity).where(
                RunEntity.idempotency_key == idempotency_key
            )
        )
        if existing is None:
            raise
        if existing.request_fingerprint != command.request_fingerprint:
            raise IdempotencyConflict(idempotency_key)
        return _record(existing), False
return _record(new_row), True
```

The PostgreSQL integration test starts two threads with separate sessions and proves one logical `run_id` is returned and only one row exists.

- [ ] **Step 4: Add the errors endpoint**

`GET /v1/runs/{run_id}/errors` returns:

```json
{
  "run_id": "run_7c73af29b37f40c59f2eea2dfef3d6ad",
  "errors": [
    {
      "attempt": 1,
      "code": "MODEL_RATE_LIMIT_EXHAUSTED",
      "source": "worker",
      "message": "Provider request failed after retries",
      "occurred_at": "2026-07-14T10:00:00Z"
    }
  ]
}
```

Never expose exception repr, request headers, provider response bodies, keys or full Prompt content.
Add an `errors` link to `_public_run()` so callers do not construct this URL themselves.

- [ ] **Step 5: Verify SQLite units and PostgreSQL concurrency**

```bash
OPENAI_API_KEY="" uv run pytest tests/service/test_repository.py tests/service/test_runs_api.py -q
HE_TEST_POSTGRES_URL="$HE_SERVICE_DATABASE_URL" OPENAI_API_KEY="" \
  uv run pytest tests/service/test_repository_postgres.py -q
uv run alembic upgrade head
uv run alembic current
```

Expected migration head: `0002_service_recovery`.

- [ ] **Step 6: Commit**

```bash
git add hyperextract/service tests/service
git commit -m "feat(service): persist run diagnostics"
```

### Task 5: Complete Cancellation, Lease Heartbeats, and Crash Recovery

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/repository.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/worker.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_repository.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_worker.py`

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_running_cancel_finishes_as_cancelled(worker, repository, cancellable_run):
    repository.request_cancel(cancellable_run.run_id)
    worker.run_once()
    assert repository.get(cancellable_run.run_id).status == "cancelled"


def test_expired_lease_requeues_same_run(repository, expired_running_run):
    recovered = repository.requeue_expired_leases(max_recoveries=3)
    record = repository.get(expired_running_run.run_id)
    assert recovered == [expired_running_run.run_id]
    assert record.status == "queued"
    assert record.resume_from_checkpoint is True


def test_active_lease_is_extended_independently_of_pipeline_events(
    repository, running_run
):
    before = running_run.lease_expires_at
    repository.renew_lease(running_run.run_id, "worker-1", lease_seconds=120)
    assert repository.lease(running_run.run_id).lease_expires_at > before
```

- [ ] **Step 2: Implement owner-checked lifecycle methods**

Add these concrete state mutations (import SQLAlchemy `select`/`update`, `WorkerHeartbeatEntity`, and `utcnow`):

```python
def mark_cancelled(self, run_id: str, worker_id: str) -> RunRecord:
    with self.session_factory.begin() as session:
        row = session.get(RunEntity, run_id, with_for_update=True)
        if row is None:
            raise KeyError(run_id)
        if row.status != "running" or row.lease_owner != worker_id:
            raise InvalidRunState(row.status)
        row.status = "cancelled"
        row.stage_status = "cancelled"
        row.lease_owner = None
        row.lease_expires_at = None
        return _record(row)


def renew_lease(self, run_id: str, worker_id: str, lease_seconds: int) -> bool:
    now = utcnow()
    with self.session_factory.begin() as session:
        result = session.execute(
            update(RunEntity)
            .where(
                RunEntity.run_id == run_id,
                RunEntity.status == "running",
                RunEntity.lease_owner == worker_id,
            )
            .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
        )
        return result.rowcount == 1


def heartbeat_worker(self, worker_id: str, version: str) -> None:
    with self.session_factory.begin() as session:
        row = session.get(WorkerHeartbeatEntity, worker_id)
        if row is None:
            session.add(
                WorkerHeartbeatEntity(
                    worker_id=worker_id,
                    version=version,
                    last_seen_at=utcnow(),
                )
            )
        else:
            row.version = version
            row.last_seen_at = utcnow()


def requeue_expired_leases(self, max_recoveries: int) -> list[str]:
    now = utcnow()
    recovered: list[str] = []
    with self.session_factory.begin() as session:
        rows = session.scalars(
            select(RunEntity)
            .where(
                RunEntity.status == "running",
                RunEntity.lease_expires_at < now,
            )
            .with_for_update(skip_locked=True)
        )
        for row in rows:
            row.lease_owner = None
            row.lease_expires_at = None
            if row.cancel_requested_at is not None:
                row.status = "cancelled"
                row.stage_status = "cancelled"
            elif row.recovery_count >= max_recoveries:
                row.status = "failed"
                row.stage_status = "failed"
                row.resumable = True
                row.error_summary_json = {
                    "code": "WORKER_RECOVERY_EXHAUSTED",
                    "message": "Worker recovery limit was reached",
                }
            else:
                row.status = "queued"
                row.stage_status = "recovering"
                row.recovery_count += 1
                row.resume_from_checkpoint = True
                recovered.append(row.run_id)
    return recovered
```

Every mutation of a running task includes `lease_owner == worker_id`. An expired run with `cancel_requested_at` becomes `cancelled`; otherwise it becomes `queued`, increments `recovery_count`, sets `resume_from_checkpoint=true`, and preserves the same `run_id`. When `recovery_count >= max_recoveries`, mark it `failed` with `WORKER_RECOVERY_EXHAUSTED` and `resumable=true`.

- [ ] **Step 3: Add an independent Worker heartbeat thread**

The Worker main loop calls `requeue_expired_leases()` before claims and reports idle heartbeats. During execution, a daemon thread renews both Worker heartbeat and task lease every `heartbeat_seconds`; if lease renewal returns false, the executor must stop at the next cancellation/control check and must not publish artifacts.

- [ ] **Step 4: Finish running cancellation explicitly**

Replace the current `RunCancelled` exception path that calls `request_cancel()` with:

```python
except RunCancelled:
    self.repository.mark_cancelled(record.run_id, self.worker_id)
```

- [ ] **Step 5: Verify recovery and commit**

```bash
OPENAI_API_KEY="" uv run pytest tests/service/test_repository.py tests/service/test_worker.py tests/service/test_pipeline_control.py -q
uv run ruff check hyperextract/service
git add hyperextract/service tests/service
git commit -m "fix(service): recover leased worker runs"
```

### Task 6: Reconcile Published Artifacts and Redact Failures

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/artifacts.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/worker.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/errors.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_artifacts.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_worker.py`

- [ ] **Step 1: Write failing crash-window tests**

```python
def test_worker_reconciles_success_marker_without_rerunning_model(
    worker, repository, published_running_run, executor
):
    worker.run_once()
    assert repository.get(published_running_run.run_id).status == "completed"
    executor.execute.assert_not_called()


def test_provider_secret_is_not_saved_in_failure(worker, repository, failed_run):
    worker.executor.execute.side_effect = RuntimeError("Bearer sk-secret-value")
    worker.run_once()
    error = repository.list_errors(failed_run.run_id)[0]
    assert "sk-secret-value" not in error.message
```

- [ ] **Step 2: Add published-artifact inspection**

```python
class ArtifactPublisher:
    def inspect_published(self, run_id: str) -> ArtifactManifest | None:
        artifacts = self.run_root / run_id / "artifacts"
        marker = artifacts / "_SUCCESS"
        manifest_path = artifacts / "artifact-manifest.json"
        if not marker.exists() and not manifest_path.exists():
            return None
        if not marker.is_file() or not manifest_path.is_file():
            raise ValueError("ARTIFACT_STATE_INCONSISTENT")
        manifest = ArtifactManifest.model_validate_json(manifest_path.read_text())
        verify_marker_manifest_hash(marker, manifest_path)
        verify_every_declared_artifact(artifacts, manifest)
        return manifest
```

Before model execution, Worker calls this method. A valid publication completes PostgreSQL without rerunning the Pipeline. Partial or invalid publication fails with `ARTIFACT_STATE_INCONSISTENT` and is never overwritten.

- [ ] **Step 3: Add stable failure normalization**

Map known model error categories to stable codes; authentication and invalid input are not resumable, transient/retry exhaustion and Worker recovery are resumable. Redact common bearer/API-key patterns and cap the public message at 500 characters. Save detailed non-secret diagnostics only under `diagnostics/attempts/`.

- [ ] **Step 4: Verify and commit**

```bash
OPENAI_API_KEY="" uv run pytest tests/service/test_artifacts.py tests/service/test_worker.py -q
uv run ruff check hyperextract/service
git add hyperextract/service tests/service
git commit -m "fix(service): reconcile artifacts after crashes"
```

### Task 7: Make Readiness Real and Synchronize the Public Documentation

**Files:**
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/api/routes/health.py`
- Modify: `/Users/king/website/Hyper-Extract/hyperextract/service/settings.py`
- Create: `/Users/king/website/Hyper-Extract/tests/service/test_readiness.py`
- Modify: `/Users/king/website/Hyper-Extract/tests/service/test_settings.py`
- Modify: `/Users/king/website/Hyper-Extract/docs/zh/guides/internal-service.md`
- Modify: `/Users/king/website/Hyper-Extract/docs/en/guides/internal-service.md`

- [ ] **Step 1: Write failing readiness tests**

```python
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
        item["check"] == "worker" for item in response.json()["error"]["details"]
    )
```

- [ ] **Step 2: Implement readiness checks**

Check:

```text
database: SELECT 1 succeeds
migration: alembic_version == expected head
package_root: directory exists and is readable
run_root: create, fsync and delete a probe file succeeds
model_profiles: configured file parses and default Profile has a public descriptor
worker: heartbeat newer than 2 * heartbeat_seconds
```

Return all failed check names in `error.details`; never include database URLs or secret values.

Remove `artifact_retention_days` and `cleanup_interval_seconds` from `ServiceSettings` because no cleanup process consumes them. First release retention remains an explicit operator policy; do not expose environment variables that appear active but are ignored.

- [ ] **Step 3: Update the API documentation from implementation truth**

Make these exact changes in both languages:

- use `contract_version="1.1"` for Package 1.1 examples;
- remove the old version-mismatch warning;
- add `GET /v1/runs/{run_id}/errors`;
- remove the running-cancel, lease-recovery and Model Profile deployment warnings after their tests pass;
- describe real readiness checks;
- retain explicit limitations for reserved execution fields, community reports and evaluation;
- retain the Brief snapshot sensitivity warning.

- [ ] **Step 4: Run the complete API gate**

```bash
cd /Users/king/website/Hyper-Extract
OPENAI_API_KEY="" uv run pytest tests/service tests/documents tests/briefs tests/methods/test_course_knowledge_graph.py -q
OPENAI_API_KEY="" uv run pytest -m "not integration" -q
uv run ruff check hyperextract
uv run ruff format --check hyperextract
uv run mkdocs build --strict
```

Expected: deterministic tests and strict docs build pass without a real model call.

- [ ] **Step 5: Commit**

```bash
git add hyperextract/service tests/service docs/zh/guides/internal-service.md docs/en/guides/internal-service.md
git commit -m "docs(service): publish hardened api contract"
```

## API Completion Gate

- HTTP-only code lives under `hyperextract/service/api`; service-core modules do not import it.
- `service/runtime.py` is shared by API and Worker, `api/app.py` contains only FastAPI composition, and `api/main.py` contains only the `he-api` Uvicorn bootstrap.
- Package 1.1 requests use `contract_version="1.1"` and mismatch is rejected.
- API and Worker read the same Model Profile definition; only Worker receives keys.
- Concurrent identical creates return one logical run.
- Running cancellation reaches `cancelled`.
- Expired leases recover the same run and checkpoint, with a bounded recovery count.
- A crash after `_SUCCESS` does not repeat model work.
- Error history is stable, queryable and redacted.
- Readiness verifies database, migration, volume, Profile and Worker.
- External docs contain no “known blocker” that this plan claims to fix.

## Cross-plan Execution Order

After completing API Tasks 1–6, execute Docker Tasks 1–4 from [Hyper-Extract Internal Service Docker Implementation Plan](./2026-07-13-internal-service-docker.md). Then return to API Task 7 to freeze the public documentation, and finish Docker Task 5 for the end-to-end deployment gate.

```text
API Tasks 1–6
→ Docker Tasks 1–4
→ API Task 7
→ Docker Task 5
```

## Prompt for a New Execution Window

```text
请在 /Users/king/website/Hyper-Extract 中实施内部 API 与 Docker 服务。

先完整阅读：
1. /Users/king/website/Hyper-Extract/AGENTS.md
2. /Users/king/website/Hyper-Extract/docs/zh/guides/internal-service.md
3. /Users/king/website/Hyper-Extract/docs/superpowers/plans/2026-07-13-internal-api-service.md
4. /Users/king/website/Hyper-Extract/docs/superpowers/plans/2026-07-13-internal-service-docker.md

使用 superpowers:executing-plans 按计划执行。

要求：
- TDD：每项先写失败测试，再实现，再运行聚焦测试。
- 所有确定性测试使用 OPENAI_API_KEY=""，不得访问真实模型。
- 保留无鉴权、无 SSE、共享卷 file:// 数据面的首期范围。
- ExtractionBrief 只位于 Document Package 1.1 内，不给 POST /v1/runs 增加内联 prompt。
- API 不持有模型密钥；只有 Worker 获得密钥与模型网络出口。
- 每个 Task 完成后检查 diff、运行测试并提交小型 conventional commit。
- 不修改或覆盖无关的用户改动。

执行顺序：API Tasks 1–6 -> Docker Tasks 1–4 -> API Task 7 -> Docker Task 5。
每个 Task 后汇报变更、测试结果和剩余风险；计划与当前代码不一致时，先给出代码证据，再做最小必要调整。
```
