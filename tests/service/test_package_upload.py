"""Tests for the HTTP Package upload contract (Task 1).

These tests lock in the new ``POST /v1/runs`` multipart contract and the
``202``/``400``/``409``/``413``/``422`` error surface BEFORE the
implementation lands. They are expected to fail (RED) until Tasks 2-4 ship.
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile

from hyperextract.documents import document_package_fingerprint

from .conftest import build_package_archive, multipart_create_payload


# ---------------------------------------------------------------------------
# 202 success and response shape
# ---------------------------------------------------------------------------


def test_multipart_create_returns_202_and_stable_links(client, package_v1_1):
    data, files = multipart_create_payload(package_v1_1)
    response = client.post(
        "/v1/runs",
        headers={"Idempotency-Key": "upload-1"},
        data=data,
        files=files,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["run_id"].startswith("run_")
    # 202 must NOT expose file:///exchange/... URIs anymore
    serialized = json.dumps(body)
    assert "file:///exchange" not in serialized
    assert "/exchange/" not in serialized
    assert "manifest_uri" not in body.get("output", {})
    assert body["links"]["self"] == f"/v1/runs/{body['run_id']}"
    assert body["links"]["result"] == f"/v1/runs/{body['run_id']}/result"
    assert body["links"]["artifacts"] == f"/v1/runs/{body['run_id']}/artifacts"


def test_multipart_create_idempotent_returns_same_run(client, package_v1_1):
    data, files = multipart_create_payload(package_v1_1)
    first = client.post(
        "/v1/runs", headers={"Idempotency-Key": "idem-1"}, data=data, files=files
    )
    assert first.status_code == 202
    second = client.post(
        "/v1/runs", headers={"Idempotency-Key": "idem-1"}, data=data, files=files
    )
    assert second.status_code == 202
    assert second.json()["run_id"] == first.json()["run_id"]


def test_multipart_create_conflict_on_changed_request(client, package_v1_1):
    data, files = multipart_create_payload(package_v1_1)
    client.post(
        "/v1/runs", headers={"Idempotency-Key": "idem-2"}, data=data, files=files
    )
    # Different options -> different request_fingerprint -> 409
    data2, files2 = multipart_create_payload(
        package_v1_1,
        options={"execution": {"context_policy": "repack"}},
    )
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "idem-2"}, data=data2, files=files2
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


# ---------------------------------------------------------------------------
# 400: missing fields / bad multipart
# ---------------------------------------------------------------------------


def test_missing_package_returns_400(client, package_v1_1):
    data, _files = multipart_create_payload(package_v1_1)
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "no-pkg"}, data=data
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PACKAGE_REQUIRED"


def test_missing_idempotency_key_rejected(client, package_v1_1):
    data, files = multipart_create_payload(package_v1_1)
    response = client.post("/v1/runs", data=data, files=files)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_MULTIPART_REQUEST"


# ---------------------------------------------------------------------------
# 422: archive / hash / fingerprint mismatches
# ---------------------------------------------------------------------------


def test_transport_hash_mismatch_returns_422(client, package_v1_1):
    archive, fingerprint, _transport = build_package_archive(package_v1_1)
    data = {
        "contract_version": "1.1",
        "package_fingerprint": fingerprint,
        "transport_sha256": "0" * 64,
    }
    files = {"package": ("course.hepkg.tar.gz", archive, "application/gzip")}
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "bad-transport"}, data=data, files=files
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PACKAGE_TRANSPORT_HASH_MISMATCH"


def test_package_fingerprint_mismatch_returns_422(client, package_v1_1):
    archive, _fingerprint, transport = build_package_archive(package_v1_1)
    data = {
        "contract_version": "1.1",
        "package_fingerprint": "0" * 64,
        "transport_sha256": transport,
    }
    files = {"package": ("course.hepkg.tar.gz", archive, "application/gzip")}
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "bad-fp"}, data=data, files=files
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DOCUMENT_PACKAGE_HASH_MISMATCH"


def test_package_fingerprint_mismatch_cleans_request_files(
    client, package_v1_1, settings
):
    archive, _fingerprint, transport = build_package_archive(package_v1_1)
    response = client.post(
        "/v1/runs",
        headers={"Idempotency-Key": "bad-fp-cleanup"},
        data={
            "contract_version": "1.1",
            "package_fingerprint": "0" * 64,
            "transport_sha256": transport,
        },
        files={"package": ("course.hepkg.tar.gz", archive, "application/gzip")},
    )
    assert response.status_code == 422
    assert list(settings.upload_root.glob(".upload-*")) == []
    assert list(settings.package_root.glob(".staging-*")) == []


def test_corrupt_gzip_returns_422(client, package_v1_1):
    _archive, fingerprint, _transport = build_package_archive(package_v1_1)
    corrupt = b"not a gzip stream"
    corrupt_transport = hashlib.sha256(corrupt).hexdigest()
    data = {
        "contract_version": "1.1",
        "package_fingerprint": fingerprint,
        "transport_sha256": corrupt_transport,
    }
    files = {"package": ("course.hepkg.tar.gz", corrupt, "application/gzip")}
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "bad-gzip"}, data=data, files=files
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PACKAGE_ARCHIVE_INVALID"


def test_archive_with_nested_top_dir_returns_422(client, package_v1_1):
    """Archive root must directly contain manifest.json, not a nested dir."""
    fingerprint = document_package_fingerprint(package_v1_1)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(package_v1_1.rglob("*")):
            if path.is_file():
                # Nest under an arbitrary top-level directory
                tar.add(path, arcname=f"nested/{path.relative_to(package_v1_1)}")
    archive = buffer.getvalue()
    transport = hashlib.sha256(archive).hexdigest()
    data = {
        "contract_version": "1.1",
        "package_fingerprint": fingerprint,
        "transport_sha256": transport,
    }
    files = {"package": ("course.hepkg.tar.gz", archive, "application/gzip")}
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "nested"}, data=data, files=files
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] in {
        "PACKAGE_ARCHIVE_INVALID",
        "DOCUMENT_PACKAGE_INVALID",
    }


# ---------------------------------------------------------------------------
# 202 must prepare run state/work/diagnostics dirs
# ---------------------------------------------------------------------------


def test_create_prepares_run_state_and_work_dirs(client, package_v1_1, settings):
    data, files = multipart_create_payload(package_v1_1)
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "dirs-1"}, data=data, files=files
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    run_dir = settings.run_root / run_id
    assert (run_dir / "work").is_dir()
    assert (run_dir / "state").is_dir()
    assert (run_dir / "diagnostics" / "attempts").is_dir()


# ---------------------------------------------------------------------------
# Malicious tar members (Task 11)
# ---------------------------------------------------------------------------


def _archive_with_member(members, fingerprint):
    """Build a tar.gz with arbitrary (member, content) pairs and return bytes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content, kind in members:
            if kind == "file":
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
            elif kind == "symlink":
                info = tarfile.TarInfo(name=name)
                info.type = tarfile.SYMTYPE
                info.linkname = content
                tar.addfile(info)
            elif kind == "hardlink":
                info = tarfile.TarInfo(name=name)
                info.type = tarfile.LNKTYPE
                info.linkname = content
                tar.addfile(info)
            elif kind == "dev":
                info = tarfile.TarInfo(name=name)
                info.type = tarfile.CHRTYPE
                tar.addfile(info)
    archive = buffer.getvalue()
    transport = hashlib.sha256(archive).hexdigest()
    data = {
        "contract_version": "1.1",
        "package_fingerprint": fingerprint,
        "transport_sha256": transport,
    }
    files = {"package": ("course.hepkg.tar.gz", archive, "application/gzip")}
    return data, files


def test_archive_rejects_absolute_path(client, package_v1_1):
    fingerprint = document_package_fingerprint(package_v1_1)
    members = [("/etc/evil", b"bad", "file")]
    data, files = _archive_with_member(members, fingerprint)
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "abs-path"}, data=data, files=files
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PACKAGE_ARCHIVE_INVALID"


def test_archive_rejects_path_traversal(client, package_v1_1):
    fingerprint = document_package_fingerprint(package_v1_1)
    members = [("../escape", b"bad", "file")]
    data, files = _archive_with_member(members, fingerprint)
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "traversal"}, data=data, files=files
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PACKAGE_ARCHIVE_INVALID"


def test_archive_rejects_symlink(client, package_v1_1):
    fingerprint = document_package_fingerprint(package_v1_1)
    members = [("manifest.json", b"{}", "file"), ("evil", "/etc/passwd", "symlink")]
    data, files = _archive_with_member(members, fingerprint)
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "symlink"}, data=data, files=files
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PACKAGE_ARCHIVE_INVALID"


def test_archive_rejects_hardlink(client, package_v1_1):
    fingerprint = document_package_fingerprint(package_v1_1)
    members = [("manifest.json", b"{}", "file"), ("evil", "manifest.json", "hardlink")]
    data, files = _archive_with_member(members, fingerprint)
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "hardlink"}, data=data, files=files
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PACKAGE_ARCHIVE_INVALID"


def test_archive_rejects_device_file(client, package_v1_1):
    fingerprint = document_package_fingerprint(package_v1_1)
    members = [("manifest.json", b"{}", "file"), ("evil", "", "dev")]
    data, files = _archive_with_member(members, fingerprint)
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "device"}, data=data, files=files
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PACKAGE_ARCHIVE_INVALID"


def test_same_fingerprint_reuses_published_package(client, package_v1_1, settings):
    """Uploading the same Package twice must reuse the content-addressed dir."""
    data, files = multipart_create_payload(package_v1_1)
    first = client.post(
        "/v1/runs", headers={"Idempotency-Key": "reuse-1"}, data=data, files=files
    )
    assert first.status_code == 202
    data2, files2 = multipart_create_payload(package_v1_1)
    second = client.post(
        "/v1/runs", headers={"Idempotency-Key": "reuse-2"}, data=data2, files=files2
    )
    assert second.status_code == 202
    # Same fingerprint → exactly one published package dir
    published = list((settings.exchange_root / "packages").glob("pkg_*.hepkg"))
    assert len(published) == 1
    assert first.json()["run_id"] != second.json()["run_id"]


def test_failed_upload_cleans_staging(client, package_v1_1, settings):
    """A failed upload must not leave staging or upload temp files behind."""
    fingerprint = document_package_fingerprint(package_v1_1)
    corrupt = b"not a gzip stream"
    transport = hashlib.sha256(corrupt).hexdigest()
    data = {
        "contract_version": "1.1",
        "package_fingerprint": fingerprint,
        "transport_sha256": transport,
    }
    files = {"package": ("course.hepkg.tar.gz", corrupt, "application/gzip")}
    client.post(
        "/v1/runs", headers={"Idempotency-Key": "cleanup"}, data=data, files=files
    )
    staging = list((settings.exchange_root / "packages").glob(".staging-*"))
    uploads = list((settings.exchange_root / "uploads").glob(".upload-*"))
    assert staging == []
    assert uploads == []
    published = list((settings.exchange_root / "packages").glob("pkg_*.hepkg"))
    assert published == []


def test_failed_upload_does_not_delete_another_requests_temp_file(
    client, package_v1_1, settings
):
    unrelated = settings.upload_root / ".upload-another-request.tar.gz"
    settings.upload_root.mkdir(parents=True, exist_ok=True)
    unrelated.write_bytes(b"still in use")
    fingerprint = document_package_fingerprint(package_v1_1)
    corrupt = b"not a gzip stream"
    response = client.post(
        "/v1/runs",
        headers={"Idempotency-Key": "isolated-cleanup"},
        data={
            "contract_version": "1.1",
            "package_fingerprint": fingerprint,
            "transport_sha256": hashlib.sha256(corrupt).hexdigest(),
        },
        files={"package": ("course.hepkg.tar.gz", corrupt, "application/gzip")},
    )

    assert response.status_code == 422
    assert unrelated.read_bytes() == b"still in use"
