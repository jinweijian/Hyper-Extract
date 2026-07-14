from __future__ import annotations

from hyperextract.documents import document_package_schemas
from hyperextract.documents.document_package import ValidatedDocumentPackage


class ServicePackageContractError(Exception):
    """Raised when a Document Package violates the public service layout."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)


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


def document_package_contract() -> dict[str, object]:
    return {
        "schema_name": "HyperExtractDocumentPackage",
        "schema_version": "1.1",
        "supported_versions": ["1.0", "1.1"],
        "required_entries": [
            {"path": "manifest.json", "kind": "file"},
            {"path": "outline.json", "kind": "file"},
            {"path": "provenance.jsonl", "kind": "file"},
            {"path": "content/", "kind": "directory"},
        ],
        "version_requirements": {
            "1.0": {"extraction_brief": "not_required"},
            "1.1": {
                "extraction_brief": "required",
                "location": "manifest.extraction_brief.path",
                "formats": ["yaml", "yml"],
            },
        },
        "schemas": document_package_schemas(),
    }
