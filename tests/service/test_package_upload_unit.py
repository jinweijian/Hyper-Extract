"""Unit tests for safe streaming upload + tar extraction (Task 2)."""
from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path

import pytest

from hyperextract.service.package_upload import (
    PackageUploadError,
    UploadLimits,
    extract_tarball,
    stream_upload,
    upload_and_extract,
)


def _limits(**overrides):
    base = dict(
        max_upload_bytes=10 * 1024 * 1024,
        max_expanded_bytes=10 * 1024 * 1024,
        max_members=1000,
        read_block=4096,
    )
    base.update(overrides)
    return UploadLimits(**base)


def _make_archive(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            if name.endswith("/"):
                info = tarfile.TarInfo(name=name)
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _archive_file(tmp_path, files, name="course.hepkg.tar.gz") -> Path:
    archive = tmp_path / name
    archive.write_bytes(_make_archive(files))
    return archive


def test_stream_upload_verifies_transport_hash(tmp_path):
    files = {"manifest.json": b"{}", "content/a.md": b"hello"}
    archive = _make_archive(files)
    expected = hashlib.sha256(archive).hexdigest()
    upload_root = tmp_path / "uploads"
    path, sha, size = stream_upload(
        io.BytesIO(archive),
        upload_root,
        expected_transport_sha256=expected,
        limits=_limits(),
    )
    assert sha == expected
    assert size == len(archive)
    assert path.is_file()


def test_stream_upload_rejects_hash_mismatch(tmp_path):
    archive = _make_archive({"manifest.json": b"{}"})
    with pytest.raises(PackageUploadError, match="TRANSPORT_HASH_MISMATCH"):
        stream_upload(
            io.BytesIO(archive),
            tmp_path / "uploads",
            expected_transport_sha256="0" * 64,
            limits=_limits(),
        )
    # temp file must be cleaned up
    assert not list((tmp_path / "uploads").iterdir()) if (tmp_path / "uploads").exists() else True


def test_stream_upload_rejects_oversized(tmp_path):
    big = b"x" * (2 * 1024 * 1024)
    archive = _make_archive({"manifest.json": big})
    expected = hashlib.sha256(archive).hexdigest()
    with pytest.raises(PackageUploadError, match="TOO_LARGE"):
        stream_upload(
            io.BytesIO(archive),
            tmp_path / "uploads",
            expected_transport_sha256=expected,
            limits=_limits(max_upload_bytes=1024),
        )


def test_stream_upload_stops_reading_when_limit_is_crossed(tmp_path):
    source = io.BytesIO(b"x" * 10_000)
    with pytest.raises(PackageUploadError, match="TOO_LARGE"):
        stream_upload(
            source,
            tmp_path / "uploads",
            expected_transport_sha256="0" * 64,
            limits=_limits(max_upload_bytes=1024, read_block=256),
        )
    assert source.tell() <= 1024 + 256
    assert list((tmp_path / "uploads").glob(".upload-*")) == []


def test_extract_regular_files(tmp_path):
    files = {"manifest.json": b"{}", "content/a.md": b"hello", "content/": b""}
    archive = _archive_file(tmp_path, files)
    staging_root = tmp_path / "packages"
    staging = extract_tarball(archive, staging_root, limits=_limits())
    assert (staging / "manifest.json").is_file()
    assert (staging / "content" / "a.md").read_text() == "hello"


def test_extract_rejects_absolute_path(tmp_path):
    # Build an archive with an absolute member name by crafting raw tar.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="/etc/evil")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"X"))
    archive = tmp_path / "evil.tar.gz"
    archive.write_bytes(buf.getvalue())
    with pytest.raises(PackageUploadError, match="ARCHIVE_INVALID"):
        extract_tarball(archive, tmp_path / "packages", limits=_limits())


def test_extract_rejects_dotdot_path(tmp_path):
    files = {"../escape.txt": b"evil"}
    archive = _archive_file(tmp_path, files)
    with pytest.raises(PackageUploadError, match="ARCHIVE_INVALID"):
        extract_tarball(archive, tmp_path / "packages", limits=_limits())


def test_extract_rejects_symlink(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="link.txt")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    archive = tmp_path / "sym.tar.gz"
    archive.write_bytes(buf.getvalue())
    with pytest.raises(PackageUploadError, match="ARCHIVE_INVALID"):
        extract_tarball(archive, tmp_path / "packages", limits=_limits())


def test_extract_rejects_hardlink(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="hard.txt")
        info.type = tarfile.LNKTYPE
        info.linkname = "/etc/shadow"
        tar.addfile(info)
    archive = tmp_path / "hard.tar.gz"
    archive.write_bytes(buf.getvalue())
    with pytest.raises(PackageUploadError, match="ARCHIVE_INVALID"):
        extract_tarball(archive, tmp_path / "packages", limits=_limits())


def test_extract_rejects_device_file(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="dev")
        info.type = tarfile.CHRTYPE
        tar.addfile(info)
    archive = tmp_path / "dev.tar.gz"
    archive.write_bytes(buf.getvalue())
    with pytest.raises(PackageUploadError, match="ARCHIVE_INVALID"):
        extract_tarball(archive, tmp_path / "packages", limits=_limits())


def test_extract_rejects_fifo(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="fifo")
        info.type = tarfile.FIFOTYPE
        tar.addfile(info)
    archive = tmp_path / "fifo.tar.gz"
    archive.write_bytes(buf.getvalue())
    with pytest.raises(PackageUploadError, match="ARCHIVE_INVALID"):
        extract_tarball(archive, tmp_path / "packages", limits=_limits())


def test_extract_rejects_too_many_members(tmp_path):
    files = {f"content/f{i}.md": b"x" for i in range(10)}
    files["manifest.json"] = b"{}"
    archive = _archive_file(tmp_path, files)
    with pytest.raises(PackageUploadError, match="TOO_LARGE"):
        extract_tarball(archive, tmp_path / "packages", limits=_limits(max_members=5))


def test_extract_rejects_expanded_too_large(tmp_path):
    big = b"x" * (1024 * 100)
    files = {"manifest.json": b"{}", "big.bin": big}
    archive = _archive_file(tmp_path, files)
    with pytest.raises(PackageUploadError, match="TOO_LARGE"):
        extract_tarball(archive, tmp_path / "packages", limits=_limits(max_expanded_bytes=1024))


def test_extract_rejects_missing_manifest(tmp_path):
    files = {"content/a.md": b"hello"}
    archive = _archive_file(tmp_path, files)
    with pytest.raises(PackageUploadError, match="manifest"):
        extract_tarball(archive, tmp_path / "packages", limits=_limits())


def test_extract_rejects_corrupt_gzip(tmp_path):
    archive = tmp_path / "corrupt.tar.gz"
    archive.write_bytes(b"not a gzip stream at all")
    with pytest.raises(PackageUploadError, match="ARCHIVE_INVALID"):
        extract_tarball(archive, tmp_path / "packages", limits=_limits())


def test_extract_failure_cleans_staging(tmp_path):
    files = {"../escape.txt": b"evil"}
    archive = _archive_file(tmp_path, files)
    staging_root = tmp_path / "packages"
    with pytest.raises(PackageUploadError):
        extract_tarball(archive, staging_root, limits=_limits())
    # No lingering staging dirs
    leftovers = [p for p in staging_root.iterdir() if p.name.startswith(".staging")]
    assert leftovers == []


def test_upload_and_extract_returns_staging(tmp_path):
    files = {"manifest.json": b"{}", "content/a.md": b"hi"}
    archive = _make_archive(files)
    expected = hashlib.sha256(archive).hexdigest()
    staged = upload_and_extract(
        io.BytesIO(archive),
        tmp_path / "uploads",
        tmp_path / "packages",
        expected_transport_sha256=expected,
        limits=_limits(),
    )
    assert (staged.staging / "manifest.json").is_file()
    assert staged.archive_path.is_file()
    assert staged.transport_sha256 == expected
