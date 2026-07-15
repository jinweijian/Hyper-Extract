from pathlib import Path
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from hyperextract.service.storage import SharedVolumeStore


def _make_staging(package_root: Path, fingerprint: str, files: dict[str, str] | None = None) -> Path:
    files = files or {}
    staging = package_root / f".staging-{fingerprint[:8]}-{uuid.uuid4().hex[:6]}"
    staging.mkdir(parents=True, exist_ok=False)
    (staging / "manifest.json").write_text(files.get("manifest.json", "{}"), encoding="utf-8")
    (staging / "content").mkdir()
    (staging / "content" / "a.md").write_text(files.get("content/a.md", "hi"), encoding="utf-8")
    return staging


def test_storage_exposes_three_roots(exchange_root):
    store = SharedVolumeStore(exchange_root)
    assert store.upload_root == (exchange_root / "uploads").resolve()
    assert store.package_root == (exchange_root / "packages").resolve()
    assert store.run_root == (exchange_root / "runs").resolve()


def test_output_locations_returns_stable_http_links(exchange_root):
    store = SharedVolumeStore(exchange_root)
    locs = store.output_locations("run_test")
    assert locs.self_link == "/v1/runs/run_test"
    assert locs.result_link == "/v1/runs/run_test/result"
    assert locs.artifacts_link == "/v1/runs/run_test/artifacts"
    assert locs.errors_link == "/v1/runs/run_test/errors"
    assert "file://" not in locs.self_link


def test_reserve_run_creates_work_state_and_diagnostics(exchange_root):
    store = SharedVolumeStore(exchange_root)
    reserved = store.reserve_run("run_test")
    assert (reserved / "work").is_dir()
    assert (reserved / "state").is_dir()
    assert (reserved / "diagnostics" / "attempts").is_dir()
    assert store.progress_path("run_test").name == "progress.json"


def test_publish_package_atomically_renames_staging(exchange_root):
    store = SharedVolumeStore(exchange_root)
    staging = _make_staging(store.package_root, "a" * 64)
    published = store.publish_package(staging, "a" * 64)
    assert published.reused is False
    assert published.path == store.package_dir("a" * 64)
    assert published.path.is_dir()
    assert (published.path / "manifest.json").is_file()
    # staging was renamed away
    assert not staging.exists()


def test_publish_package_reuses_identical_existing(exchange_root, package_v1_1):
    store = SharedVolumeStore(exchange_root)
    from hyperextract.documents import document_package_fingerprint

    fingerprint = document_package_fingerprint(package_v1_1)
    staging1 = store.package_root / ".staging-first"
    staging2 = store.package_root / ".staging-second"
    store.package_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(package_v1_1, staging1)
    shutil.copytree(package_v1_1, staging2)
    published1 = store.publish_package(staging1, fingerprint)
    published2 = store.publish_package(staging2, fingerprint)
    assert published2.reused is True
    assert published2.path == published1.path
    assert not staging2.exists()


def test_publish_package_handles_concurrent_identical_publishers(
    exchange_root, package_v1_1
):
    from hyperextract.documents import document_package_fingerprint

    store = SharedVolumeStore(exchange_root)
    fingerprint = document_package_fingerprint(package_v1_1)
    store.package_root.mkdir(parents=True, exist_ok=True)
    staging_paths = [
        store.package_root / ".staging-concurrent-first",
        store.package_root / ".staging-concurrent-second",
    ]
    for staging in staging_paths:
        shutil.copytree(package_v1_1, staging)
    barrier = Barrier(2)

    def publish(staging: Path):
        barrier.wait()
        return store.publish_package(staging, fingerprint)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, staging_paths))

    assert {result.path for result in results} == {store.package_dir(fingerprint)}
    assert sorted(result.reused for result in results) == [False, True]
    assert all(not staging.exists() for staging in staging_paths)


def test_publish_package_rejects_same_size_tampered_existing(
    exchange_root, package_v1_1
):
    from hyperextract.documents import document_package_fingerprint

    store = SharedVolumeStore(exchange_root)
    fingerprint = document_package_fingerprint(package_v1_1)
    store.package_root.mkdir(parents=True, exist_ok=True)
    first = store.package_root / ".staging-first"
    second = store.package_root / ".staging-second"
    shutil.copytree(package_v1_1, first)
    shutil.copytree(package_v1_1, second)
    published = store.publish_package(first, fingerprint)
    content_path = next((published.path / "content").glob("*.md"))
    original = content_path.read_bytes()
    content_path.write_bytes(b"x" * len(original))

    with pytest.raises(ValueError, match="PACKAGE_STATE_INCONSISTENT"):
        store.publish_package(second, fingerprint)


def test_publish_package_rejects_mismatched_content(exchange_root):
    store = SharedVolumeStore(exchange_root)
    staging1 = _make_staging(store.package_root, "c" * 64)
    store.publish_package(staging1, "c" * 64)
    # Different content under same fingerprint
    staging2 = _make_staging(
        store.package_root, "c" * 64, files={"manifest.json": '{"different":true}'}
    )
    with pytest.raises(ValueError, match="PACKAGE_STATE_INCONSISTENT"):
        store.publish_package(staging2, "c" * 64)
    # Existing package untouched
    assert (store.package_dir("c" * 64) / "manifest.json").read_text() == "{}"


def test_resolve_package_ref_rejects_staging_and_missing(exchange_root):
    store = SharedVolumeStore(exchange_root)
    with pytest.raises(ValueError, match="PATH_FORBIDDEN"):
        store.resolve_package_ref("0" * 64)
    # Publish a real one
    staging = _make_staging(store.package_root, "d" * 64)
    store.publish_package(staging, "d" * 64)
    resolved = store.resolve_package_ref("d" * 64)
    assert resolved.is_dir()
    assert resolved.name == "pkg_" + "d" * 64 + ".hepkg"


def test_resolve_package_ref_rejects_bad_fingerprint(exchange_root):
    store = SharedVolumeStore(exchange_root)
    with pytest.raises(ValueError, match="INVALID_PACKAGE_FINGERPRINT"):
        store.resolve_package_ref("../escape")
    with pytest.raises(ValueError, match="INVALID_PACKAGE_FINGERPRINT"):
        store.resolve_package_ref("")


def test_discard_reserved_run_removes_state(exchange_root):
    store = SharedVolumeStore(exchange_root)
    store.reserve_run("run_discard")
    assert store.run_dir("run_discard").exists()
    store.discard_reserved_run("run_discard")
    assert not store.run_dir("run_discard").exists()
