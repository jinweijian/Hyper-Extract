from fastapi import APIRouter, Depends

from hyperextract.documents import (
    document_package_fingerprint,
    validate_document_package,
)
from hyperextract.service.contracts import document_package_contract
from hyperextract.service.errors import ServiceError
from hyperextract.service.runtime import ServiceRuntime

from ..dependencies import get_runtime
from ..schemas import ValidatePackageRequest

router = APIRouter()


@router.get("/v1/capabilities")
def capabilities() -> dict:
    return {
        "pipelines": ["course_graph"],
        "document_package_versions": ["1.0", "1.1"],
        "package_schemes": ["file"],
        "lifecycle": ["create", "status", "cancel", "resume", "artifacts"],
    }


@router.get("/v1/contracts/document-package/v1")
def package_contract() -> dict:
    return document_package_contract()


@router.post("/v1/document-packages/validate")
def validate_package(
    payload: ValidatePackageRequest,
    runtime: ServiceRuntime = Depends(get_runtime),
) -> dict:
    try:
        package = runtime.storage.resolve_package_uri(payload.package_uri)
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
