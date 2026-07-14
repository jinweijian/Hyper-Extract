from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from hyperextract.documents import (
    document_package_fingerprint,
    validate_document_package,
)
from hyperextract.documents.checkpoint import fingerprint

from .contracts import document_package_contract
from .db import create_engine_and_session
from .errors import ServiceError
from .model_profiles import ModelProfileRegistry
from .repository import IdempotencyConflict, InvalidRunState, RunRecord, RunRepository
from .schemas import RunCommand, RunCreateRequest, ValidatePackageRequest
from .settings import ServiceSettings
from .storage import OutputLocations, SharedVolumeStore


@dataclass(frozen=True)
class ServiceContext:
    settings: ServiceSettings
    repository: RunRepository
    storage: SharedVolumeStore
    model_profiles: ModelProfileRegistry


def get_context(request: Request) -> ServiceContext:
    return request.app.state.context


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex


def _public_run(record: RunRecord, locations: OutputLocations) -> dict:
    return {
        "run_id": record.run_id,
        "status": record.status,
        "stage": record.stage,
        "stage_status": record.stage_status,
        "attempt": record.attempt,
        "progress": record.progress,
        "error_summary": record.error_summary,
        "resumable": record.resumable,
        "cancel_requested": record.cancel_requested,
        "output": locations.__dict__,
        "links": {
            "self": f"/v1/runs/{record.run_id}",
            "cancel": f"/v1/runs/{record.run_id}/cancel",
            "resume": f"/v1/runs/{record.run_id}/resume",
            "artifacts": f"/v1/runs/{record.run_id}/artifacts",
        },
    }


def _run_or_404(context: ServiceContext, run_id: str) -> RunRecord:
    record = context.repository.get(run_id)
    if record is None:
        raise ServiceError(404, "RUN_NOT_FOUND", "Run was not found")
    return record


def resolve_run_command(
    request: RunCreateRequest,
    package: Path,
    package_fingerprint: str,
    context: ServiceContext,
) -> RunCommand:
    payload = request.model_dump(mode="json")
    payload["resolved_package_path"] = str(package)
    payload["resolved_package_fingerprint"] = package_fingerprint
    try:
        model_descriptor = context.model_profiles.public_descriptor(
            request.execution.model_profile
        )
    except (KeyError, ValueError) as error:
        raise ServiceError(
            422,
            "MODEL_PROFILE_INVALID",
            f"Model profile is unavailable: {request.execution.model_profile}",
        ) from error
    payload["resolved_config"] = {
        "pipeline_version": 3,
        "profile_name": request.pipeline.profile.name,
        "profile_version": request.pipeline.profile.version,
        "model_profile_name": request.execution.model_profile,
        "model_profile_fingerprint": model_descriptor["fingerprint"],
    }
    request_fingerprint = fingerprint(payload)
    run_id = new_run_id()
    output = context.storage.output_locations(run_id)
    return RunCommand(
        run_id=run_id,
        request_fingerprint=request_fingerprint,
        request_json=payload,
        output_uri=output.run_uri,
    )


def create_app(
    settings: ServiceSettings | None = None,
    repository: RunRepository | None = None,
    model_profiles: ModelProfileRegistry | None = None,
) -> FastAPI:
    resolved = settings or ServiceSettings.from_env()
    owned_engine = None
    if repository is None:
        owned_engine, session_factory = create_engine_and_session(resolved.database_url)
        repository = RunRepository(session_factory)
    context = ServiceContext(
        settings=resolved,
        repository=repository,
        storage=SharedVolumeStore(resolved.exchange_root),
        model_profiles=model_profiles
        or ModelProfileRegistry(resolved.model_profiles_path),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        resolved.package_root.mkdir(parents=True, exist_ok=True)
        resolved.run_root.mkdir(parents=True, exist_ok=True)
        yield
        if owned_engine is not None:
            owned_engine.dispose()

    app = FastAPI(
        title="Hyper-Extract Internal Service", version="1.0", lifespan=lifespan
    )
    app.state.context = context

    @app.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, error: ServiceError):
        return JSONResponse(status_code=error.status_code, content=error.body())

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> dict:
        checks = {
            "package_root_readable": resolved.package_root.is_dir(),
            "run_root_writable": resolved.run_root.is_dir(),
            "repository": True,
        }
        if not all(checks.values()):
            raise ServiceError(503, "SERVICE_NOT_READY", "Service checks failed")
        return {"status": "ready", "checks": checks}

    @app.get("/v1/capabilities")
    def capabilities() -> dict:
        return {
            "pipelines": ["course_graph"],
            "document_package_versions": ["1.0", "1.1"],
            "package_schemes": ["file"],
            "lifecycle": ["create", "status", "cancel", "resume", "artifacts"],
        }

    @app.get("/v1/contracts/document-package/v1")
    def package_contract() -> dict:
        return document_package_contract()

    @app.post("/v1/document-packages/validate")
    def validate_package(
        payload: ValidatePackageRequest,
        context: ServiceContext = Depends(get_context),
    ) -> dict:
        try:
            package = context.storage.resolve_package_uri(payload.package_uri)
            validated = validate_document_package(package)
            actual = document_package_fingerprint(package)
        except ValueError as error:
            raise ServiceError(422, "DOCUMENT_PACKAGE_INVALID", str(error)) from error
        if actual != payload.sha256:
            raise ServiceError(
                422,
                "DOCUMENT_PACKAGE_HASH_MISMATCH",
                "Document Package fingerprint does not match the request",
            )
        return {
            "valid": True,
            "sha256": actual,
            "schema_version": validated.manifest.schema_version,
            "document_id": validated.manifest.document.id,
            "content_count": len(validated.manifest.contents),
            "extraction_brief": (
                {
                    "id": validated.extraction_brief.metadata.id,
                    "version": validated.extraction_brief.metadata.version,
                    "content_hash": validated.extraction_brief.content_hash,
                }
                if validated.extraction_brief is not None
                else None
            ),
        }

    @app.post("/v1/runs", status_code=202)
    def create_run(
        payload: RunCreateRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ],
        context: ServiceContext = Depends(get_context),
    ) -> dict:
        try:
            package = context.storage.resolve_package_uri(payload.input.package_uri)
            actual = document_package_fingerprint(package)
        except ValueError as error:
            raise ServiceError(422, "DOCUMENT_PACKAGE_INVALID", str(error)) from error
        if actual != payload.input.sha256:
            raise ServiceError(
                422,
                "DOCUMENT_PACKAGE_HASH_MISMATCH",
                "Document Package fingerprint does not match the request",
            )
        command = resolve_run_command(payload, package, actual, context)
        context.storage.reserve_run(command.run_id)
        try:
            record, created = context.repository.create_or_get(command, idempotency_key)
        except IdempotencyConflict as error:
            context.storage.discard_reserved_run(command.run_id)
            raise ServiceError(
                409,
                "IDEMPOTENCY_KEY_CONFLICT",
                "Idempotency key was already used for a different request",
            ) from error
        except Exception:
            context.storage.discard_reserved_run(command.run_id)
            raise
        if not created:
            context.storage.discard_reserved_run(command.run_id)
        return _public_run(record, context.storage.output_locations(record.run_id))

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, context: ServiceContext = Depends(get_context)) -> dict:
        record = _run_or_404(context, run_id)
        return _public_run(record, context.storage.output_locations(run_id))

    @app.post("/v1/runs/{run_id}/cancel")
    def cancel_run(run_id: str, context: ServiceContext = Depends(get_context)) -> dict:
        _run_or_404(context, run_id)
        try:
            record = context.repository.request_cancel(run_id)
        except InvalidRunState as error:
            raise ServiceError(
                409, "RUN_NOT_CANCELLABLE", "Run cannot be cancelled"
            ) from error
        return _public_run(record, context.storage.output_locations(run_id))

    @app.post("/v1/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: str, context: ServiceContext = Depends(get_context)) -> dict:
        _run_or_404(context, run_id)
        try:
            record = context.repository.resume(run_id)
        except InvalidRunState as error:
            raise ServiceError(
                409,
                "RUN_NOT_RESUMABLE",
                "Run cannot be resumed from its current state",
            ) from error
        return _public_run(record, context.storage.output_locations(run_id))

    @app.get("/v1/runs/{run_id}/artifacts")
    def artifacts(run_id: str, context: ServiceContext = Depends(get_context)) -> dict:
        record = _run_or_404(context, run_id)
        if record.status != "completed":
            raise ServiceError(
                409, "ARTIFACTS_NOT_READY", "Run artifacts are not ready"
            )
        manifest = (
            context.settings.run_root / run_id / "artifacts" / "artifact-manifest.json"
        )
        if not manifest.exists():
            raise ServiceError(
                500, "ARTIFACT_STATE_INCONSISTENT", "Artifact manifest is missing"
            )
        import json

        return json.loads(manifest.read_text(encoding="utf-8"))

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        "hyperextract.service.app:create_app", factory=True, host="0.0.0.0", port=8000
    )
