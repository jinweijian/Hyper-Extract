import hashlib
import json
from pathlib import Path

import pytest

from hyperextract.documents.document_package import (
    document_package_fingerprint,
    load_document_package,
    validate_document_package,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_package(root: Path) -> Path:
    content = root / "content"
    content.mkdir(parents=True)
    body = "## 2.1 Topic\n\nDefinition alpha.\n"
    noise = "## Index\n\nalpha, 10\n"
    (content / "body.md").write_text(body, encoding="utf-8")
    (content / "index.md").write_text(noise, encoding="utf-8")
    outline = {
        "schema_name": "HyperExtractOutline",
        "schema_version": "1.0",
        "nodes": [
            {
                "id": "root",
                "title": "Course",
                "depth": 0,
                "parent_id": None,
                "order": 0,
                "source_refs": [],
            },
            {
                "id": "section-2-1",
                "title": "2.1 Topic",
                "depth": 1,
                "parent_id": "root",
                "order": 1,
                "source_refs": [{"ref": "source.md#L10-L12"}],
            },
        ],
    }
    provenance = [
        {"content_id": "body", "source_refs": [{"ref": "source.md#L10-L12"}]},
        {"content_id": "index", "source_refs": [{"ref": "source.md#L30-L31"}]},
    ]
    manifest = {
        "schema_name": "HyperExtractDocumentPackage",
        "schema_version": "1.0",
        "document": {"id": "course", "title": "Course", "language": "en"},
        "producer": {"name": "test-adapter", "version": "1.0"},
        "outline_path": "outline.json",
        "provenance_path": "provenance.jsonl",
        "contents": [
            {
                "id": "body",
                "path": "content/body.md",
                "order": 0,
                "content_kind": "body",
                "outline_id": "section-2-1",
                "sha256": _sha256(body),
                "bytes": len(body.encode("utf-8")),
                "extract": True,
            },
            {
                "id": "index",
                "path": "content/index.md",
                "order": 1,
                "content_kind": "index",
                "outline_id": None,
                "sha256": _sha256(noise),
                "bytes": len(noise.encode("utf-8")),
                "extract": False,
            },
        ],
    }
    (root / "outline.json").write_text(json.dumps(outline), encoding="utf-8")
    (root / "provenance.jsonl").write_text(
        "".join(f"{json.dumps(item)}\n" for item in provenance), encoding="utf-8"
    )
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _manifest(root: Path) -> dict:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _replace_manifest(root: Path, manifest: dict) -> None:
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_load_document_package_uses_outline_and_only_extractable_content(tmp_path):
    root = _write_package(tmp_path / "course.hepkg")

    outline, blocks = load_document_package(root)

    assert outline.schema_name == "HyperExtractOutline"
    assert [node.title for node in outline.nodes] == ["Course", "2.1 Topic"]
    assert [block.text for block in blocks] == ["## 2.1 Topic\n\nDefinition alpha.\n"]
    assert blocks[0].outline_path == ["2.1 Topic"]
    assert blocks[0].top_level_id == "section-2-1"
    assert blocks[0].source_refs[0].source_path == "source.md"
    assert blocks[0].source_refs[0].start_line == 10


@pytest.mark.parametrize("path", ["../outside.md", "/tmp/outside.md"])
def test_document_package_rejects_unsafe_paths(tmp_path, path):
    root = _write_package(tmp_path / "course.hepkg")
    manifest = _manifest(root)
    manifest["contents"][0]["path"] = path
    _replace_manifest(root, manifest)

    with pytest.raises(ValueError, match="unsafe path"):
        validate_document_package(root)


def test_document_package_rejects_symlinks(tmp_path):
    root = _write_package(tmp_path / "course.hepkg")
    target = root / "content" / "body.md"
    outside = tmp_path / "outside.md"
    outside.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(ValueError, match="symbolic link"):
        validate_document_package(root)


def test_document_package_rejects_hash_or_size_mismatch(tmp_path):
    root = _write_package(tmp_path / "course.hepkg")
    (root / "content" / "body.md").write_text("tampered", encoding="utf-8")

    with pytest.raises(ValueError, match="hash|byte size"):
        validate_document_package(root)


def test_document_package_rejects_unsupported_version_and_missing_files(tmp_path):
    root = _write_package(tmp_path / "course.hepkg")
    manifest = _manifest(root)
    manifest["schema_version"] = "2.0"
    _replace_manifest(root, manifest)
    with pytest.raises(ValueError, match="schema_version"):
        validate_document_package(root)

    root = _write_package(tmp_path / "missing.hepkg")
    (root / "content" / "body.md").unlink()
    with pytest.raises(ValueError, match="unsafe path"):
        validate_document_package(root)


@pytest.mark.parametrize("field", ["id", "path", "order"])
def test_document_package_rejects_duplicate_content_identity(tmp_path, field):
    root = _write_package(tmp_path / "course.hepkg")
    manifest = _manifest(root)
    manifest["contents"][1][field] = manifest["contents"][0][field]
    _replace_manifest(root, manifest)

    with pytest.raises(ValueError, match="duplicate"):
        validate_document_package(root)


def test_document_package_enforces_resource_limits(tmp_path):
    from hyperextract.documents.document_package import DocumentPackageLimits

    root = _write_package(tmp_path / "course.hepkg")
    with pytest.raises(ValueError, match="file count"):
        validate_document_package(root, DocumentPackageLimits(max_files=1))
    with pytest.raises(ValueError, match="size limit"):
        validate_document_package(root, DocumentPackageLimits(max_file_bytes=3))
    with pytest.raises(ValueError, match="total size"):
        validate_document_package(root, DocumentPackageLimits(max_total_bytes=10))


@pytest.mark.parametrize("failure", ["orphan", "cycle", "content-outline"])
def test_document_package_rejects_invalid_outline_references(tmp_path, failure):
    root = _write_package(tmp_path / "course.hepkg")
    outline_path = root / "outline.json"
    outline = json.loads(outline_path.read_text(encoding="utf-8"))
    manifest = _manifest(root)
    if failure == "orphan":
        outline["nodes"][1]["parent_id"] = "missing"
    elif failure == "cycle":
        outline["nodes"][0]["parent_id"] = "section-2-1"
    else:
        manifest["contents"][0]["outline_id"] = "missing"
    outline_path.write_text(json.dumps(outline), encoding="utf-8")
    _replace_manifest(root, manifest)

    with pytest.raises(ValueError, match="outline|cycle|parent"):
        validate_document_package(root)


def test_document_package_fingerprint_is_stable_and_content_sensitive(tmp_path):
    root = _write_package(tmp_path / "course.hepkg")
    first = document_package_fingerprint(root)
    second = document_package_fingerprint(root)
    assert first == second

    body_path = root / "content" / "body.md"
    body = body_path.read_text(encoding="utf-8") + "More.\n"
    body_path.write_text(body, encoding="utf-8")
    manifest = _manifest(root)
    manifest["contents"][0]["sha256"] = _sha256(body)
    manifest["contents"][0]["bytes"] = len(body.encode("utf-8"))
    _replace_manifest(root, manifest)

    assert document_package_fingerprint(root) != first
