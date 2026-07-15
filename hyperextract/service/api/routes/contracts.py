from fastapi import APIRouter, Depends

from hyperextract.documents import (
    document_package_fingerprint,
    validate_document_package,
)
from hyperextract.service.contracts import (
    ServicePackageContractError,
    document_package_contract,
    validate_service_package_layout,
)
from hyperextract.service.errors import ServiceError
from hyperextract.service.runtime import ServiceRuntime

from ..dependencies import get_runtime
from ..schemas import ResultMetadataResponse, RunResponse, ValidatePackageRequest

router = APIRouter()


@router.get("/v1/capabilities")
def capabilities() -> dict:
    return {
        "pipelines": ["course_graph"],
        "document_package_versions": ["1.0", "1.1"],
        "package_schemes": ["http-multipart"],
        "timeline_schema_versions": ["1.0"],
        "contracts": {
            "run_status": "/v1/contracts/run-status/v1",
            "result_metadata": "/v1/contracts/result-metadata/v1",
        },
        "lifecycle": [
            "create",
            "status",
            "cancel",
            "resume",
            "artifacts",
            "result",
            "result-metadata",
        ],
    }


@router.get("/v1/contracts/document-package/v1")
def package_contract() -> dict:
    return document_package_contract()


@router.get("/v1/contracts/run-status/v1")
def run_status_contract() -> dict:
    return RunResponse.model_json_schema()


@router.get("/v1/contracts/result-metadata/v1")
def result_metadata_contract() -> dict:
    return ResultMetadataResponse.model_json_schema()


@router.post("/v1/document-packages/validate")
def validate_package(
    payload: ValidatePackageRequest,
    runtime: ServiceRuntime = Depends(get_runtime),
) -> dict:
    try:
        package = runtime.storage.resolve_package_ref(payload.package_fingerprint)
        validated = validate_document_package(package)
        validate_service_package_layout(validated, payload.contract_version)
        actual = document_package_fingerprint(package)
    except ServicePackageContractError as error:
        raise ServiceError(422, error.code, error.message) from error
    except ValueError as error:
        raise ServiceError(422, "DOCUMENT_PACKAGE_INVALID", str(error)) from error
    if actual != payload.package_fingerprint:
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
