from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header

from hyperextract.documents import document_package_fingerprint, validate_document_package
from hyperextract.documents.checkpoint import fingerprint
from hyperextract.service.commands import RunCommand
from hyperextract.service.contracts import (
    ServicePackageContractError,
    validate_service_package_layout,
)
from hyperextract.service.errors import ServiceError
from hyperextract.service.repository import (
    IdempotencyConflict,
    InvalidRunState,
    RunRecord,
    RunRepository,
)
from hyperextract.service.runtime import ServiceRuntime
from hyperextract.service.storage import OutputLocations

from ..dependencies import get_runtime
from ..schemas import RunCreateRequest
from ..schemas.responses import OutputResponse, RunLinksResponse, RunResponse

router = APIRouter()


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex


def _public_run(record: RunRecord, locations: OutputLocations) -> RunResponse:
    return RunResponse(
        run_id=record.run_id,
        status=record.status,
        stage=record.stage,
        stage_status=record.stage_status,
        attempt=record.attempt,
        progress=record.progress,
        error_summary=record.error_summary,
        resumable=record.resumable,
        cancel_requested=record.cancel_requested,
        output=OutputResponse(
            run_uri=locations.run_uri,
            artifacts_uri=locations.artifacts_uri,
            manifest_uri=locations.manifest_uri,
            success_marker_uri=locations.success_marker_uri,
        ),
        links=RunLinksResponse(
            self=f"/v1/runs/{record.run_id}",
            cancel=f"/v1/runs/{record.run_id}/cancel",
            resume=f"/v1/runs/{record.run_id}/resume",
            errors=f"/v1/runs/{record.run_id}/errors",
            artifacts=f"/v1/runs/{record.run_id}/artifacts",
        ),
    )


def _run_or_404(repository: RunRepository, run_id: str) -> RunRecord:
    record = repository.get(run_id)
    if record is None:
        raise ServiceError(404, "RUN_NOT_FOUND", "Run was not found")
    return record


def resolve_run_command(
    request: RunCreateRequest,
    package: Path,
    package_fingerprint: str,
    runtime: ServiceRuntime,
) -> RunCommand:
    payload = request.model_dump(mode="json")
    payload["resolved_package_path"] = str(package)
    payload["resolved_package_fingerprint"] = package_fingerprint
    try:
        model_descriptor = runtime.model_profiles.public_descriptor(
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
    output = runtime.storage.output_locations(run_id)
    return RunCommand(
        run_id=run_id,
        request_fingerprint=request_fingerprint,
        request_json=payload,
        output_uri=output.run_uri,
    )


@router.post("/v1/runs", status_code=202, response_model=RunResponse)
def create_run(
    payload: RunCreateRequest,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=255)
    ],
    runtime: ServiceRuntime = Depends(get_runtime),
) -> RunResponse:
    try:
        package = runtime.storage.resolve_package_uri(payload.input.package_uri)
        validated = validate_document_package(package)
        validate_service_package_layout(validated, payload.input.contract_version)
        actual = document_package_fingerprint(package)
    except ServicePackageContractError as error:
        raise ServiceError(422, error.code, error.message) from error
    except ValueError as error:
        raise ServiceError(422, "DOCUMENT_PACKAGE_INVALID", str(error)) from error
    if actual != payload.input.sha256:
        raise ServiceError(
            422,
            "DOCUMENT_PACKAGE_HASH_MISMATCH",
            "Document Package fingerprint does not match the request",
        )
    command = resolve_run_command(payload, package, actual, runtime)
    runtime.storage.reserve_run(command.run_id)
    try:
        record, created = runtime.repository.create_or_get(command, idempotency_key)
    except IdempotencyConflict as error:
        runtime.storage.discard_reserved_run(command.run_id)
        raise ServiceError(
            409,
            "IDEMPOTENCY_KEY_CONFLICT",
            "Idempotency key was already used for a different request",
        ) from error
    except Exception:
        runtime.storage.discard_reserved_run(command.run_id)
        raise
    if not created:
        runtime.storage.discard_reserved_run(command.run_id)
    return _public_run(record, runtime.storage.output_locations(record.run_id))


@router.get("/v1/runs/{run_id}", response_model=RunResponse)
def get_run(
    run_id: str, runtime: ServiceRuntime = Depends(get_runtime)
) -> RunResponse:
    record = _run_or_404(runtime.repository, run_id)
    return _public_run(record, runtime.storage.output_locations(run_id))


@router.post("/v1/runs/{run_id}/cancel", response_model=RunResponse)
def cancel_run(
    run_id: str, runtime: ServiceRuntime = Depends(get_runtime)
) -> RunResponse:
    _run_or_404(runtime.repository, run_id)
    try:
        record = runtime.repository.request_cancel(run_id)
    except InvalidRunState as error:
        raise ServiceError(409, "RUN_NOT_CANCELLABLE", "Run cannot be cancelled") from error
    return _public_run(record, runtime.storage.output_locations(run_id))


@router.post("/v1/runs/{run_id}/resume", status_code=202, response_model=RunResponse)
def resume_run(
    run_id: str, runtime: ServiceRuntime = Depends(get_runtime)
) -> RunResponse:
    _run_or_404(runtime.repository, run_id)
    try:
        record = runtime.repository.resume(run_id)
    except InvalidRunState as error:
        raise ServiceError(
            409,
            "RUN_NOT_RESUMABLE",
            "Run cannot be resumed from its current state",
        ) from error
    return _public_run(record, runtime.storage.output_locations(run_id))


@router.get("/v1/runs/{run_id}/artifacts")
def artifacts(
    run_id: str, runtime: ServiceRuntime = Depends(get_runtime)
) -> dict:
    record = _run_or_404(runtime.repository, run_id)
    if record.status != "completed":
        raise ServiceError(409, "ARTIFACTS_NOT_READY", "Run artifacts are not ready")
    manifest = (
        runtime.settings.run_root / run_id / "artifacts" / "artifact-manifest.json"
    )
    if not manifest.exists():
        raise ServiceError(
            500, "ARTIFACT_STATE_INCONSISTENT", "Artifact manifest is missing"
        )
    return json.loads(manifest.read_text(encoding="utf-8"))
