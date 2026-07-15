"""Streaming upload and safe tar extraction for ``POST /v1/runs``.

This module turns an uploaded ``.hepkg.tar.gz`` byte stream into a staging
directory on the shared exchange volume. It is deliberately decoupled from
FastAPI so it can be unit-tested with plain file objects.

Security contract (section 1.3 / 1.4 of the plan):
  * Stream the upload to disk and compute SHA-256 incrementally — never hold
    the whole archive in memory.
  * Reject archives whose declared transport SHA-256 does not match the bytes.
  * Validate every tar member explicitly on both Python 3.11 and 3.12 — do not
    rely on version-dependent ``extractall()`` defaults.
  * Reject absolute paths, ``..`` segments, empty paths, duplicate target
    paths, symlinks, hardlinks, device files, FIFOs, and members that exceed
    the size/count limits.
  * Only regular files and directories are materialised, and every resolved
    target must stay inside the staging root.
  * On any failure the upload temp file and staging tree are removed; an
    existing published Package is never touched.
"""

from __future__ import annotations

import hashlib
import shutil
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path


class PackageUploadError(Exception):
    """Raised with a stable error ``code`` and HTTP-facing message."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class UploadLimits:
    max_upload_bytes: int
    max_expanded_bytes: int
    max_members: int
    read_block: int


@dataclass(frozen=True)
class StagedPackage:
    """Result of a successful upload + extraction.

    ``staging`` is the directory that directly contains the Document Package
    files (``manifest.json`` etc.). The caller atomically publishes it to the
    final content-addressed Package directory.
    """

    staging: Path
    archive_path: Path
    transport_sha256: str
    archive_bytes: int


def _sha256_stream(
    source, dest: Path, block: int, *, max_bytes: int
) -> tuple[str, int]:
    """Stream ``source`` into ``dest`` while computing SHA-256 and size."""
    digest = hashlib.sha256()
    total = 0
    with dest.open("wb") as handle:
        while True:
            chunk = source.read(block)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise PackageUploadError("PACKAGE_UPLOAD_TOO_LARGE")
            digest.update(chunk)
            handle.write(chunk)
        handle.flush()
    return digest.hexdigest(), total


def stream_upload(
    source,
    upload_root: Path,
    *,
    expected_transport_sha256: str,
    limits: UploadLimits,
) -> tuple[Path, str, int]:
    """Stream an upload to ``upload_root`` and verify its transport hash.

    ``source`` is any binary file-like object (e.g. ``UploadFile.file``).
    Returns ``(archive_path, transport_sha256, size)``. Raises
    :class:`PackageUploadError` on size or hash violations. The caller is
    responsible for deleting ``archive_path`` once the Package is published.
    """
    upload_root.mkdir(parents=True, exist_ok=True)
    archive_path = upload_root / f".upload-{uuid.uuid4().hex}.tar.gz"
    try:
        transport_sha256, size = _sha256_stream(
            source,
            archive_path,
            limits.read_block,
            max_bytes=limits.max_upload_bytes,
        )
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    if transport_sha256 != expected_transport_sha256:
        archive_path.unlink(missing_ok=True)
        raise PackageUploadError("PACKAGE_TRANSPORT_HASH_MISMATCH")
    return archive_path, transport_sha256, size


def _is_within(staging: Path, target: Path) -> bool:
    try:
        target.relative_to(staging)
    except ValueError:
        return False
    return True


def _safe_member_name(name: str) -> str:
    """Reject unsafe tar member names; return a normalised relative path."""
    if not name or name in {".", "./"}:
        raise PackageUploadError("PACKAGE_ARCHIVE_INVALID", "empty tar member name")
    # Normalise without resolving symlinks, then reject any escape.
    normalised = Path(name).as_posix()
    if normalised.startswith("/"):
        raise PackageUploadError("PACKAGE_ARCHIVE_INVALID", "absolute tar member path")
    if ".." in Path(normalised).parts:
        raise PackageUploadError("PACKAGE_ARCHIVE_INVALID", "tar member escapes root")
    return normalised


def extract_tarball(
    archive_path: Path,
    staging_root: Path,
    *,
    limits: UploadLimits,
) -> Path:
    """Safely extract ``archive_path`` into a fresh staging directory.

    Returns the staging directory whose root directly contains the Document
    Package files. Raises :class:`PackageUploadError` on any safety or limit
    violation; the staging directory is removed on failure.
    """
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = staging_root / f".staging-{uuid.uuid4().hex}"
    staging.mkdir()

    seen_targets: set[Path] = set()
    total_bytes = 0
    member_count = 0
    succeeded = False
    try:
        with tarfile.open(archive_path, mode="r:gz") as tar:
            for member in tar:
                member_count += 1
                if member_count > limits.max_members:
                    raise PackageUploadError("PACKAGE_EXPANDED_TOO_LARGE")
                if member.issym() or member.islnk():
                    raise PackageUploadError(
                        "PACKAGE_ARCHIVE_INVALID", "symlink/hardlink members forbidden"
                    )
                if member.isdev() or member.isfifo():
                    raise PackageUploadError(
                        "PACKAGE_ARCHIVE_INVALID", "device/fifo members forbidden"
                    )
                relative = _safe_member_name(member.name)
                target = (staging / relative).resolve()
                if not _is_within(staging.resolve(), target):
                    raise PackageUploadError(
                        "PACKAGE_ARCHIVE_INVALID", "tar member escapes staging root"
                    )
                # Reject duplicate targets (case-sensitivity collisions).
                if target in seen_targets:
                    raise PackageUploadError(
                        "PACKAGE_ARCHIVE_INVALID", "duplicate tar member target"
                    )
                seen_targets.add(target)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isreg():
                    raise PackageUploadError(
                        "PACKAGE_ARCHIVE_INVALID", "non-regular tar member"
                    )
                total_bytes += member.size
                if total_bytes > limits.max_expanded_bytes:
                    raise PackageUploadError("PACKAGE_EXPANDED_TOO_LARGE")
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=limits.read_block)
        manifest = staging / "manifest.json"
        if not manifest.is_file():
            raise PackageUploadError(
                "PACKAGE_ARCHIVE_INVALID", "archive root must contain manifest.json"
            )
        succeeded = True
        return staging
    except tarfile.TarError as error:
        raise PackageUploadError(
            "PACKAGE_ARCHIVE_INVALID", f"tar archive is corrupt: {error}"
        ) from error
    except PackageUploadError:
        raise
    except Exception as error:
        raise PackageUploadError(
            "PACKAGE_ARCHIVE_INVALID", f"failed to extract archive: {error}"
        ) from error
    finally:
        if not succeeded:
            shutil.rmtree(staging, ignore_errors=True)


def cleanup_archive(archive_path: Path) -> None:
    """Best-effort removal of the upload temp file."""
    try:
        archive_path.unlink(missing_ok=True)
    except OSError:
        pass


def upload_and_extract(
    source,
    upload_root: Path,
    staging_root: Path,
    *,
    expected_transport_sha256: str,
    limits: UploadLimits,
) -> StagedPackage:
    """Stream an upload, verify it, and safely extract it to staging.

    On any failure the upload temp file and staging tree are removed. On
    success the archive temp file is left in place so the caller can audit
    size; it should be deleted once the Package is published.
    """
    archive_path, transport_sha256, size = stream_upload(
        source,
        upload_root,
        expected_transport_sha256=expected_transport_sha256,
        limits=limits,
    )
    try:
        staging = extract_tarball(archive_path, staging_root, limits=limits)
    except Exception:
        cleanup_archive(archive_path)
        raise
    return StagedPackage(
        staging=staging,
        archive_path=archive_path,
        transport_sha256=transport_sha256,
        archive_bytes=size,
    )
