from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, Response, UploadFile

from hyperextract.documents import (
    document_package_fingerprint,
    validate_document_package,
)
from hyperextract.service.commands import RunCommand
from hyperextract.service.contracts import (
    ServicePackageContractError,
    validate_service_package_layout,
)
from hyperextract.service.errors import ServiceError
from hyperextract.service.package_upload import (
    PackageUploadError,
    UploadLimits,
    upload_and_extract,
)
from hyperextract.service.repository import (
    IdempotencyConflict,
    InvalidRunState,
    RunRecord,
    RunRepository,
)
from hyperextract.service.runtime import ServiceRuntime

from ..dependencies import get_runtime
from ..schemas.requests import RunOptions
from ..schemas.responses import (
    ErrorEntryResponse,
    ProgressResponse,
    RunErrorsResponse,
    RunLinksResponse,
    RunResponse,
    ResultMetadataResponse,
    TimelineStepResponse,
)

router = APIRouter()


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex


def _public_run(record: RunRecord, runtime: ServiceRuntime) -> RunResponse:
    (
        activity,
        message,
        message_seq,
        progress_view,
        progress_updated_at,
        timeline_state,
    ) = _merged_progress(record, runtime)
    locations = runtime.storage.output_locations(record.run_id)
    return RunResponse(
        run_id=record.run_id,
        status=record.status,
        stage=record.stage,
        stage_status=record.stage_status,
        attempt=record.attempt,
        activity=activity,
        message=message,
        message_seq=message_seq,
        progress=progress_view,
        timeline_schema_version=timeline_state.schema_version,
        timeline=[_timeline_step_response(step) for step in timeline_state.steps],
        error_summary=record.error_summary,
        resumable=record.resumable,
        cancel_requested=record.cancel_requested,
        updated_at=progress_updated_at or record.updated_at,
        links=RunLinksResponse(
            self=locations.self_link,
            result=locations.result_link,
            result_metadata=locations.result_metadata_link,
            artifacts=locations.artifacts_link,
            errors=locations.errors_link,
            cancel=locations.self_link + "/cancel",
            resume=locations.self_link + "/resume",
        ),
    )


def _merged_progress(record: RunRecord, runtime: ServiceRuntime):
    """Merge DB lifecycle with the structured progress file.

    The DB is the source of truth for status/attempt/lease. When the run is
    ``running`` we additionally read ``state/progress.json`` and only accept it
    if its ``worker_id`` matches the current lease owner. Otherwise we degrade
    to a stable, stage-appropriate message and ``progress=null``.
    """
    from hyperextract.service import progress as progress_mod
    from hyperextract.service import timeline as timeline_mod

    fallback_activity, fallback_message = _fallback_for_status(record)
    timeline_state = timeline_mod.read_timeline(
        runtime.storage.timeline_path(record.run_id)
    )
    timeline_valid = timeline_mod.is_valid_for_run(
        timeline_state,
        run_id=record.run_id,
        attempt=record.attempt,
        worker_id=record.lease_owner,
        require_owner=record.status == "running",
    )
    if not timeline_valid:
        timeline_state = timeline_mod.fallback_timeline(
            run_id=record.run_id,
            attempt=record.attempt,
            status=record.status,
            db_stage=record.stage,
            message=(
                str(record.error_summary.get("message", ""))
                if record.error_summary
                else fallback_message
            ),
        )
    elif (
        record.status == "running" and timeline_mod.current_step(timeline_state) is None
    ):
        timeline_state = timeline_mod.transition(
            timeline_state,
            activity=timeline_mod.PIPELINE_STAGE_ACTIVITY.get(record.stage),
            status="running",
            message=fallback_message,
        )
    elif (
        record.status in {"failed", "cancelled"}
        and timeline_mod.current_step(timeline_state) is None
    ):
        timeline_state = timeline_mod.transition(
            timeline_state,
            activity=timeline_mod.PIPELINE_STAGE_ACTIVITY.get(record.stage),
            status="failed",
            message=(
                str(record.error_summary.get("message", ""))
                if record.error_summary
                else fallback_message
            ),
        )

    if record.status != "running":
        selected = timeline_mod.current_step(timeline_state)
        if selected is not None:
            return (
                selected.activity,
                selected.message or fallback_message,
                selected.message_seq,
                _timeline_progress_response(selected.progress),
                None,
                timeline_state,
            )
        return fallback_activity, fallback_message, 0, None, None, timeline_state

    snapshot = progress_mod.read_snapshot(runtime.storage.progress_path(record.run_id))
    if not progress_mod.is_owner_valid(
        snapshot,
        run_id=record.run_id,
        attempt=record.attempt,
        lease_owner=record.lease_owner,
        max_age_seconds=max(15.0, runtime.settings.progress_seconds * 4),
    ):
        selected = timeline_mod.current_step(timeline_state)
        if selected is not None:
            return (
                selected.activity,
                selected.message or fallback_message,
                selected.message_seq,
                _timeline_progress_response(selected.progress),
                None,
                timeline_state,
            )
        return fallback_activity, fallback_message, 0, None, None, timeline_state
    selected = timeline_mod.current_step(timeline_state)
    if selected is None or selected.activity != snapshot.activity:
        timeline_state = timeline_mod.ensure_running_activity(
            timeline_state,
            activity=snapshot.activity,
            message=snapshot.message,
        )
    timeline_state = timeline_mod.overlay_current(
        timeline_state,
        activity=snapshot.activity,
        message=snapshot.message,
        message_seq=snapshot.sequence,
        current=snapshot.current,
        total=snapshot.total,
    )
    selected = timeline_mod.current_step(timeline_state)
    try:
        snapshot_updated_at = datetime.fromisoformat(snapshot.updated_at)
    except ValueError:
        snapshot_updated_at = None
    return (
        selected.activity if selected is not None else fallback_activity,
        (selected.message if selected is not None else snapshot.message),
        (selected.message_seq if selected is not None else snapshot.sequence),
        (
            _timeline_progress_response(selected.progress)
            if selected is not None
            else ProgressResponse(
                current=snapshot.current,
                total=snapshot.total,
                percent=snapshot.percent,
            )
        ),
        snapshot_updated_at,
        timeline_state,
    )


def _timeline_progress_response(value):
    if value is None:
        return None
    return ProgressResponse(
        current=value.current,
        total=value.total,
        percent=value.percent,
    )


def _timeline_step_response(step):
    return TimelineStepResponse(
        activity=step.activity,
        label=step.label,
        status=step.status,
        message=step.message,
        message_seq=step.message_seq,
        progress=_timeline_progress_response(step.progress),
        started_at=step.started_at,
        completed_at=step.completed_at,
        attempt=step.attempt,
    )


def _fallback_for_status(record: RunRecord):
    from hyperextract.service import progress as progress_mod

    if record.status == "queued":
        return "RUN_QUEUED", progress_mod.ACTIVITY_MESSAGES["RUN_QUEUED"]
    if record.stage_status == "recovering":
        return (
            "WORKER_RECOVERING",
            progress_mod.ACTIVITY_MESSAGES["WORKER_RECOVERING"],
        )
    if record.status == "completed":
        return "RUN_COMPLETED", progress_mod.ACTIVITY_MESSAGES["RUN_COMPLETED"]
    if record.status == "cancelled":
        return "RUN_CANCELLED", progress_mod.ACTIVITY_MESSAGES["RUN_CANCELLED"]
    if record.status == "failed":
        return "RUN_FAILED", progress_mod.ACTIVITY_MESSAGES["RUN_FAILED"]
    return "EXTRACTING_CHUNK", "正在执行知识抽取"


def _run_or_404(repository: RunRepository, run_id: str) -> RunRecord:
    record = repository.get(run_id)
    if record is None:
        raise ServiceError(404, "RUN_NOT_FOUND", "Run was not found")
    return record


def _upload_limits(runtime: ServiceRuntime) -> UploadLimits:
    s = runtime.settings
    return UploadLimits(
        max_upload_bytes=s.max_upload_bytes,
        max_expanded_bytes=s.max_expanded_bytes,
        max_members=s.max_archive_members,
        read_block=s.upload_read_block,
    )


_UPLOAD_ERROR_STATUS = {
    "PACKAGE_UPLOAD_TOO_LARGE": 413,
    "PACKAGE_EXPANDED_TOO_LARGE": 422,
    "PACKAGE_TRANSPORT_HASH_MISMATCH": 422,
    "PACKAGE_ARCHIVE_INVALID": 422,
}


def _status_for_upload_error(code: str) -> int:
    return _UPLOAD_ERROR_STATUS.get(code, 422)


def resolve_run_command(
    options: RunOptions,
    package_fingerprint: str,
    runtime: ServiceRuntime,
) -> RunCommand:
    try:
        validate_profile = getattr(runtime.model_profiles, "validate", None)
        if callable(validate_profile):
            validate_profile(
                options.execution.model_profile,
                require_secrets=False,
                require_embedder=True,
                check_probe=True,
            )
        model_descriptor = runtime.model_profiles.public_descriptor(
            options.execution.model_profile
        )
    except (KeyError, ValueError) as error:
        raise ServiceError(
            422,
            "MODEL_PROFILE_INVALID",
            f"Model profile is unavailable: {options.execution.model_profile}",
        ) from error
    payload = {
        "pipeline": options.pipeline.model_dump(mode="json"),
        "execution": options.execution.model_dump(mode="json"),
        "client_context": options.client_context.model_dump(mode="json"),
        "resolved_package_ref": package_fingerprint,
        "resolved_config": {
            "pipeline_version": 3,
            "profile_name": options.pipeline.profile.name,
            "profile_version": options.pipeline.profile.version,
            "model_profile_name": options.execution.model_profile,
            "model_profile_fingerprint": model_descriptor["fingerprint"],
        },
    }
    request_fingerprint = _fingerprint(payload)
    run_id = new_run_id()
    output = runtime.storage.output_locations(run_id)
    return RunCommand(
        run_id=run_id,
        request_fingerprint=request_fingerprint,
        request_json=payload,
        output_uri=output.self_link,
        resolved_package_fingerprint=package_fingerprint,
    )


def _fingerprint(payload: dict) -> str:
    from hyperextract.documents.checkpoint import fingerprint

    return fingerprint(payload)


@router.post("/v1/runs", status_code=202, response_model=RunResponse)
def create_run(
    contract_version: Annotated[str, Form()],
    package_fingerprint: Annotated[str, Form(pattern=r"^[0-9a-f]{64}$")],
    transport_sha256: Annotated[str, Form(pattern=r"^[0-9a-f]{64}$")],
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=255)
    ],
    package: Annotated[
        UploadFile | None, File(description=".hepkg.tar.gz archive")
    ] = None,
    options: Annotated[str | None, Form()] = None,
    runtime: ServiceRuntime = Depends(get_runtime),
) -> RunResponse:
    if package is None:
        raise ServiceError(400, "PACKAGE_REQUIRED", "package archive is required")
    # 1. Parse options JSON (strict).
    try:
        options_text = options or "{}"
        # Merge required defaults: pipeline is required.
        parsed = json.loads(options_text)
    except (json.JSONDecodeError, TypeError) as error:
        raise ServiceError(
            400, "INVALID_MULTIPART_REQUEST", f"options is not valid JSON: {error}"
        ) from error
    if not isinstance(parsed, dict):
        raise ServiceError(
            400, "INVALID_MULTIPART_REQUEST", "options must be a JSON object"
        )
    # Inject default pipeline if caller omitted it (matches contract: options
    # is optional with course-graph defaults).
    parsed.setdefault(
        "pipeline",
        {
            "name": "course_graph",
            "profile": {"name": "course_knowledge_graph", "version": "1"},
        },
    )
    try:
        run_options = RunOptions.model_validate(parsed)
    except Exception as error:
        raise ServiceError(
            400, "INVALID_MULTIPART_REQUEST", f"options failed validation: {error}"
        ) from error
    if contract_version != "1.1":
        raise ServiceError(
            422, "DOCUMENT_PACKAGE_VERSION_MISMATCH", "Unsupported contract version"
        )

    # 2. Stream upload + safe extraction to staging.
    limits = _upload_limits(runtime)
    try:
        staged = upload_and_extract(
            package.file,
            runtime.settings.upload_root,
            runtime.settings.package_root,
            expected_transport_sha256=transport_sha256,
            limits=limits,
        )
    except PackageUploadError as error:
        raise ServiceError(
            status_code=_status_for_upload_error(error.code),
            code=error.code,
            message=error.message,
        ) from error

    try:
        # 3. Validate Document Package v1.1 contract + fingerprint.
        try:
            validated = validate_document_package(staged.staging)
            validate_service_package_layout(validated, contract_version)
            actual_fingerprint = document_package_fingerprint(staged.staging)
        except ServicePackageContractError as error:
            raise ServiceError(422, error.code, error.message) from error
        except ValueError as error:
            raise ServiceError(422, "DOCUMENT_PACKAGE_INVALID", str(error)) from error
        if actual_fingerprint != package_fingerprint:
            raise ServiceError(
                422,
                "DOCUMENT_PACKAGE_HASH_MISMATCH",
                "Document Package fingerprint does not match the request",
            )

        # 4. Atomically publish the Package (content-addressed).
        try:
            runtime.storage.publish_package(staged.staging, actual_fingerprint)
        except ValueError as error:
            raise ServiceError(500, "PACKAGE_PUBLICATION_FAILED", str(error)) from error
    finally:
        # Request-owned cleanup: never scan/delete another upload request's files.
        from hyperextract.service.package_upload import cleanup_archive

        cleanup_archive(staged.archive_path)
        if staged.staging.exists():
            shutil.rmtree(staged.staging, ignore_errors=True)

    # 5. Build the run command and reserve run dirs.
    command = resolve_run_command(run_options, actual_fingerprint, runtime)
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
    response.headers["Location"] = runtime.storage.output_locations(
        record.run_id
    ).self_link
    response.headers["Retry-After"] = "2"
    return _public_run(record, runtime)


@router.get("/v1/runs/{run_id}", response_model=RunResponse)
def get_run(
    run_id: str,
    response: Response,
    runtime: ServiceRuntime = Depends(get_runtime),
) -> RunResponse:
    record = _run_or_404(runtime.repository, run_id)
    response.headers["Cache-Control"] = "no-store"
    return _public_run(record, runtime)


@router.post("/v1/runs/{run_id}/cancel", response_model=RunResponse)
def cancel_run(
    run_id: str, runtime: ServiceRuntime = Depends(get_runtime)
) -> RunResponse:
    _run_or_404(runtime.repository, run_id)
    try:
        record = runtime.repository.request_cancel(run_id)
    except InvalidRunState as error:
        raise ServiceError(
            409, "RUN_NOT_CANCELLABLE", "Run cannot be cancelled"
        ) from error
    return _public_run(record, runtime)


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
    return _public_run(record, runtime)


@router.get("/v1/runs/{run_id}/artifacts")
def artifacts(run_id: str, runtime: ServiceRuntime = Depends(get_runtime)) -> dict:
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


@router.get("/v1/runs/{run_id}/result")
def get_result(run_id: str, runtime: ServiceRuntime = Depends(get_runtime)):
    """Stream the fixed ``course-graph.json`` artifact (Task 8)."""
    record = _run_or_404(runtime.repository, run_id)
    if record.status != "completed":
        raise ServiceError(409, "ARTIFACTS_NOT_READY", "Run result is not ready")
    artifacts_dir = runtime.settings.run_root / run_id / "artifacts"
    manifest_path = artifacts_dir / "artifact-manifest.json"
    success = artifacts_dir / "_SUCCESS"
    if not manifest_path.is_file() or not success.is_file():
        raise ServiceError(
            500, "ARTIFACT_STATE_INCONSISTENT", "Artifact publication is incomplete"
        )
    from hyperextract.service.artifacts import ArtifactPublisher

    publisher = ArtifactPublisher(runtime.settings.run_root)
    try:
        manifest = publisher.inspect_published(run_id)
    except ValueError as error:
        raise ServiceError(500, "ARTIFACT_STATE_INCONSISTENT", str(error)) from error
    course_entry = next(
        (e for e in manifest.artifacts if e.name == "course_graph"), None
    )
    if course_entry is None:
        raise ServiceError(
            500, "ARTIFACT_STATE_INCONSISTENT", "course-graph artifact missing"
        )
    course_path = artifacts_dir / course_entry.path
    if not course_path.is_file():
        raise ServiceError(
            500, "ARTIFACT_STATE_INCONSISTENT", "course-graph file missing"
        )
    from fastapi.responses import StreamingResponse

    def _stream():
        with course_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        _stream(),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="course-graph-{run_id}.json"',
            "Content-Length": str(course_entry.size),
            "ETag": f'"{course_entry.sha256}"',
            "Cache-Control": "private, no-transform",
        },
    )


@router.get(
    "/v1/runs/{run_id}/result-metadata",
    response_model=ResultMetadataResponse,
)
def get_result_metadata(
    run_id: str, runtime: ServiceRuntime = Depends(get_runtime)
) -> ResultMetadataResponse:
    """Return a small, sanitized projection of validated result artifacts."""
    record = _run_or_404(runtime.repository, run_id)
    if record.status != "completed":
        raise ServiceError(409, "ARTIFACTS_NOT_READY", "Run result is not ready")

    from hyperextract.service.artifacts import ArtifactPublisher

    publisher = ArtifactPublisher(runtime.settings.run_root)
    try:
        manifest = publisher.inspect_published(run_id)
        if manifest is None:
            raise ValueError("publication missing")
        entries = {entry.name: entry for entry in manifest.artifacts}
        required = {
            "course_graph",
            "run_summary",
            "quality_report",
            "performance_report",
        }
        if not required.issubset(entries):
            raise ValueError("required metadata artifact missing")
        artifacts_dir = runtime.settings.run_root / run_id / "artifacts"
        summary = _read_metadata_json(artifacts_dir / entries["run_summary"].path)
        quality = _read_metadata_json(artifacts_dir / entries["quality_report"].path)
        performance = _read_metadata_json(
            artifacts_dir / entries["performance_report"].path
        )
        marker = _read_metadata_json(artifacts_dir / "_SUCCESS")
        if marker.get("run_id") != run_id or summary.get("run_id") != run_id:
            raise ValueError("run identity mismatch")
        relation_distribution = quality["relation_distribution"]
        dangling_edges = quality["dangling_edges"]
        if not isinstance(relation_distribution, dict) or not isinstance(
            dangling_edges, list
        ):
            raise ValueError("invalid quality report")
        course = entries["course_graph"]
        return ResultMetadataResponse.model_validate(
            {
                "run_id": run_id,
                "completed_at": marker["completed_at"],
                "profile": summary["profile"],
                "extraction_brief": summary.get("extraction_brief"),
                "artifact": {
                    "media_type": course.media_type,
                    "schema_name": course.schema_name,
                    "size_bytes": course.size,
                    "sha256": course.sha256,
                },
                "performance": {
                    "elapsed_seconds": performance["wall_elapsed_seconds"],
                    "chunk_count": performance["chunks"],
                },
                "quality": {
                    "outline_sections": quality["outline_sections"],
                    "extractable_sections": quality["extractable_sections"],
                    "covered_sections": quality["covered_sections"],
                    "directly_covered_sections": quality["directly_covered_sections"],
                    "hierarchically_covered_sections": quality[
                        "hierarchically_covered_sections"
                    ],
                    "outline_coverage": quality["outline_coverage"],
                    "uncovered_section_ids": quality["uncovered_section_ids"],
                    "knowledge_points": quality["knowledge_points"],
                    "relations": quality["relations"],
                    "relation_distribution": {
                        name: relation_distribution[name]
                        for name in (
                            "prerequisite",
                            "derivative",
                            "related",
                            "confusable",
                        )
                    },
                    "dangling_edge_count": len(dangling_edges),
                    "passed": quality["passed"],
                },
            }
        )
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise ServiceError(
            500,
            "ARTIFACT_STATE_INCONSISTENT",
            "Published result metadata is incomplete or invalid",
        ) from error


def _read_metadata_json(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("metadata artifact must be an object")
    return value


@router.get("/v1/runs/{run_id}/errors", response_model=RunErrorsResponse)
def list_run_errors(
    run_id: str, runtime: ServiceRuntime = Depends(get_runtime)
) -> RunErrorsResponse:
    """Return the attempt/error history for ``run_id``.

    The response is redacted at the repository boundary: ``details_json`` is
    never surfaced, so callers cannot see exception repr, request headers,
    provider response bodies, keys, or full Prompt content.
    """
    _run_or_404(runtime.repository, run_id)
    errors = [
        ErrorEntryResponse(
            attempt=error.attempt,
            code=error.code,
            source=error.source,
            message=error.message,
            occurred_at=error.occurred_at,
        )
        for error in runtime.repository.list_errors(run_id)
    ]
    return RunErrorsResponse(run_id=run_id, errors=errors)
