"""Validated reader for parser-neutral Hyper-Extract Document Packages."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hyperextract.briefs import ExtractionBrief, load_extraction_brief

from .models import DocumentOutline, OutlineNode, SourceBlock, SourceReference


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentPackageLimits(_ContractModel):
    max_files: int = Field(default=10_000, ge=1)
    max_file_bytes: int = Field(default=64 * 1024 * 1024, ge=1)
    max_total_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1)


class PackageSource(_ContractModel):
    path: str
    sha256: str | None = None


class PackageDocument(_ContractModel):
    id: str
    title: str
    language: str = ""
    source: PackageSource | None = None


class PackageProducer(_ContractModel):
    name: str
    version: str


class PackageArtifact(_ContractModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0, le=256 * 1024)


ContentKind = Literal[
    "body",
    "table_of_contents",
    "appendix",
    "references",
    "index",
    "front_matter",
    "back_matter",
    "other",
]


class PackageContent(_ContractModel):
    id: str
    path: str
    order: int = Field(ge=0)
    content_kind: ContentKind
    outline_id: str | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)
    extract: bool

    @model_validator(mode="after")
    def extraction_requires_outline(self) -> PackageContent:
        if self.extract and not self.outline_id:
            raise ValueError("extractable content requires outline_id")
        return self


class PackageManifest(_ContractModel):
    schema_name: Literal["HyperExtractDocumentPackage"]
    schema_version: Literal["1.0", "1.1"]
    document: PackageDocument
    producer: PackageProducer
    outline_path: str
    provenance_path: str
    extraction_brief: PackageArtifact | None = None
    contents: list[PackageContent]

    @model_validator(mode="after")
    def require_extraction_brief_for_v1_1(self) -> "PackageManifest":
        if self.schema_version == "1.1" and self.extraction_brief is None:
            raise ValueError(
                "Document Package v1.1 requires extraction_brief inside the package"
            )
        return self


class PackageOutlineNode(_ContractModel):
    id: str
    title: str
    depth: int = Field(ge=0)
    parent_id: str | None = None
    order: int = Field(ge=0)
    source_refs: list[SourceReference] = Field(default_factory=list)


class PackageOutline(_ContractModel):
    schema_name: Literal["HyperExtractOutline"]
    schema_version: Literal["1.0"]
    nodes: list[PackageOutlineNode]


class PackageProvenance(_ContractModel):
    content_id: str
    source_refs: list[SourceReference]


@dataclass(frozen=True)
class ValidatedDocumentPackage:
    root: Path
    manifest: PackageManifest
    outline: PackageOutline
    provenance: dict[str, PackageProvenance]
    content_bytes: dict[str, bytes]
    extraction_brief: ExtractionBrief | None
    extraction_brief_bytes: bytes | None


def document_package_schemas() -> dict[str, dict]:
    """Expose the public v1 contract without leaking private implementation types."""
    manifest_schema = PackageManifest.model_json_schema()
    manifest_schema.setdefault("allOf", []).append(
        {
            "if": {
                "properties": {"schema_version": {"const": "1.1"}},
                "required": ["schema_version"],
            },
            "then": {
                "required": ["extraction_brief"],
                "properties": {"extraction_brief": {"not": {"type": "null"}}},
            },
        }
    )
    return {
        "manifest": manifest_schema,
        "outline": PackageOutline.model_json_schema(),
        "provenance": PackageProvenance.model_json_schema(),
        "extraction_brief": ExtractionBrief.model_json_schema(),
    }


_LINE_REF = re.compile(r"^(?P<path>.+)#L(?P<start>\d+)-L(?P<end>\d+)$")


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {label}: expected an object")
    return value


def _safe_file(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ValueError(f"Document Package contains unsafe path: {relative}")
    root_resolved = root.resolve()
    target = root.joinpath(candidate)
    current = root
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Document Package path uses a symbolic link: {relative}")
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(root_resolved)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        raise ValueError(
            f"Document Package contains unsafe path: {relative}"
        ) from error
    if not resolved.is_file():
        raise ValueError(f"Document Package path is not a file: {relative}")
    return resolved


def _unique(values: list[str | int], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"Document Package contains duplicate {label}")


def _validate_outline(outline: PackageOutline) -> dict[str, PackageOutlineNode]:
    if not outline.nodes:
        raise ValueError("Document Package outline is empty")
    _unique([node.id for node in outline.nodes], "outline id")
    _unique([node.order for node in outline.nodes], "outline order")
    nodes = {node.id: node for node in outline.nodes}
    roots = [node for node in outline.nodes if node.parent_id is None]
    if len(roots) != 1:
        raise ValueError("Document Package outline must contain exactly one root")
    if roots[0].depth != 0:
        raise ValueError("Document Package outline root depth must be zero")

    for node in outline.nodes:
        if node.parent_id is None:
            continue
        parent = nodes.get(node.parent_id)
        if parent is None:
            raise ValueError(
                f"Document Package outline parent not found: {node.parent_id}"
            )
        if node.depth != parent.depth + 1:
            raise ValueError(f"Document Package outline depth mismatch: {node.id}")
        visited = {node.id}
        cursor = parent
        while cursor.parent_id is not None:
            if cursor.id in visited:
                raise ValueError(f"Document Package outline cycle detected: {node.id}")
            visited.add(cursor.id)
            next_node = nodes.get(cursor.parent_id)
            if next_node is None:
                raise ValueError(
                    f"Document Package outline parent not found: {cursor.parent_id}"
                )
            cursor = next_node
        if cursor.id in visited:
            raise ValueError(f"Document Package outline cycle detected: {node.id}")
    return nodes


def _hydrate_reference(reference: SourceReference, content_id: str) -> SourceReference:
    update: dict[str, str | int] = {}
    match = _LINE_REF.match(reference.ref)
    if match:
        if reference.source_path is None:
            update["source_path"] = match.group("path")
        if reference.start_line is None:
            update["start_line"] = int(match.group("start"))
        if reference.end_line is None:
            update["end_line"] = int(match.group("end"))
    if reference.content_id is None:
        update["content_id"] = content_id
    return reference.model_copy(update=update)


def validate_document_package(
    path: str | Path,
    limits: DocumentPackageLimits | None = None,
) -> ValidatedDocumentPackage:
    """Validate every declared package artifact before returning any content."""
    root = Path(path)
    if not root.is_dir():
        raise ValueError(f"Document Package directory not found: {root}")
    limits = limits or DocumentPackageLimits()

    manifest_path = _safe_file(root, "manifest.json")
    try:
        manifest = PackageManifest.model_validate(
            _read_json(manifest_path, "manifest.json")
        )
    except Exception as error:
        raise ValueError(f"Invalid Document Package manifest: {error}") from error
    if len(manifest.contents) > limits.max_files:
        raise ValueError("Document Package exceeds the file count limit")
    _unique([item.id for item in manifest.contents], "content id")
    _unique([item.path for item in manifest.contents], "content path")
    _unique([item.order for item in manifest.contents], "content order")

    outline_path = _safe_file(root, manifest.outline_path)
    provenance_path = _safe_file(root, manifest.provenance_path)
    try:
        outline = PackageOutline.model_validate(
            _read_json(outline_path, "outline.json")
        )
    except Exception as error:
        raise ValueError(f"Invalid Document Package outline: {error}") from error
    outline_nodes = _validate_outline(outline)

    provenance_items: list[PackageProvenance] = []
    try:
        for line_number, line in enumerate(
            provenance_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.strip():
                provenance_items.append(PackageProvenance.model_validate_json(line))
    except Exception as error:
        raise ValueError(f"Invalid Document Package provenance: {error}") from error
    _unique([item.content_id for item in provenance_items], "provenance content id")
    provenance = {item.content_id: item for item in provenance_items}

    expected_ids = {item.id for item in manifest.contents}
    if set(provenance) != expected_ids:
        raise ValueError(
            "Document Package provenance content ids do not match manifest"
        )

    content_bytes: dict[str, bytes] = {}
    total_bytes = outline_path.stat().st_size + provenance_path.stat().st_size
    extraction_brief: ExtractionBrief | None = None
    extraction_brief_bytes: bytes | None = None
    if manifest.extraction_brief is not None:
        declaration = manifest.extraction_brief
        brief_path = _safe_file(root, declaration.path)
        if brief_path.suffix.lower() not in {".yaml", ".yml"}:
            raise ValueError("Document Package ExtractionBrief must be YAML")
        extraction_brief_bytes = brief_path.read_bytes()
        if len(extraction_brief_bytes) != declaration.bytes:
            raise ValueError("Document Package ExtractionBrief byte size mismatch")
        if hashlib.sha256(extraction_brief_bytes).hexdigest() != declaration.sha256:
            raise ValueError("Document Package ExtractionBrief hash mismatch")
        extraction_brief = load_extraction_brief(brief_path)
        total_bytes += len(extraction_brief_bytes)
    for item in manifest.contents:
        if item.outline_id is not None and item.outline_id not in outline_nodes:
            raise ValueError(
                f"Document Package content references unknown outline: {item.outline_id}"
            )
        content_path = _safe_file(root, item.path)
        data = content_path.read_bytes()
        if len(data) != item.bytes:
            raise ValueError(f"Document Package byte size mismatch: {item.path}")
        if len(data) > limits.max_file_bytes:
            raise ValueError(f"Document Package file exceeds size limit: {item.path}")
        if hashlib.sha256(data).hexdigest() != item.sha256:
            raise ValueError(f"Document Package hash mismatch: {item.path}")
        total_bytes += len(data)
        if total_bytes > limits.max_total_bytes:
            raise ValueError("Document Package exceeds total size limit")
        content_bytes[item.id] = data

    return ValidatedDocumentPackage(
        root=root.resolve(),
        manifest=manifest,
        outline=outline,
        provenance=provenance,
        content_bytes=content_bytes,
        extraction_brief=extraction_brief,
        extraction_brief_bytes=extraction_brief_bytes,
    )


def _outline_path(
    node_id: str,
    nodes: dict[str, PackageOutlineNode],
) -> tuple[list[str], str]:
    chain: list[PackageOutlineNode] = []
    cursor = nodes[node_id]
    while cursor.parent_id is not None:
        chain.append(cursor)
        cursor = nodes[cursor.parent_id]
    chain.reverse()
    return [node.title for node in chain], chain[0].id if chain else node_id


def load_document_package(
    path: str | Path,
) -> tuple[DocumentOutline, list[SourceBlock]]:
    """Load a validated parser-neutral package into HE's internal document models."""
    package = validate_document_package(path)
    package_nodes = {node.id: node for node in package.outline.nodes}
    outline = DocumentOutline(
        document_name=package.manifest.document.title,
        schema_name=package.outline.schema_name,
        schema_version=package.outline.schema_version,
        nodes=[
            OutlineNode(
                id=node.id,
                title=node.title,
                level=node.depth,
                parent_id=node.parent_id,
                order=node.order,
                source_refs=node.source_refs,
            )
            for node in sorted(package.outline.nodes, key=lambda item: item.order)
        ],
    )
    blocks: list[SourceBlock] = []
    for item in sorted(package.manifest.contents, key=lambda entry: entry.order):
        if not item.extract or item.outline_id is None:
            continue
        path_titles, top_level_id = _outline_path(item.outline_id, package_nodes)
        references = [
            _hydrate_reference(reference, item.id)
            for reference in package.provenance[item.id].source_refs
        ]
        blocks.append(
            SourceBlock(
                id=item.id,
                kind=item.content_kind,
                text=package.content_bytes[item.id].decode("utf-8"),
                outline_id=item.outline_id,
                outline_path=path_titles,
                top_level_id=top_level_id,
                source_refs=references,
            )
        )
    if not blocks:
        raise ValueError("Document Package contains no extractable content")
    return outline, blocks


def load_package_extraction_brief(path: str | Path) -> ExtractionBrief | None:
    """Return the validated package-owned ExtractionBrief, when declared."""
    return validate_document_package(path).extraction_brief


def document_package_fingerprint(path: str | Path) -> str:
    """Return a stable content fingerprint after strict package validation."""
    package = validate_document_package(path)
    manifest_payload = package.manifest.model_dump(mode="json")
    if package.manifest.extraction_brief is None:
        manifest_payload.pop("extraction_brief", None)
    payload = {
        "manifest": manifest_payload,
        "outline": package.outline.model_dump(mode="json"),
        "provenance": [
            package.provenance[item.id].model_dump(mode="json")
            for item in sorted(package.manifest.contents, key=lambda entry: entry.order)
        ],
    }
    if package.extraction_brief is not None:
        payload["extraction_brief"] = package.extraction_brief.model_dump(mode="json")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
