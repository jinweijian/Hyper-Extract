from __future__ import annotations

import shutil
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OutputLocations:
    """Stable HTTP-relative links for a run (never ``file://`` URIs)."""

    run_id: str
    self_link: str
    result_link: str
    result_metadata_link: str
    artifacts_link: str
    errors_link: str


@dataclass(frozen=True)
class PublishedPackage:
    """A content-addressed Document Package on the shared volume."""

    path: Path
    fingerprint: str
    reused: bool


def _contains_symlink(path: Path, stop: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == stop or current.parent == current:
            return False
        current = current.parent


class SharedVolumeStore:
    """Owns the three exchange sub-roots: uploads, packages, runs.

    The store no longer accepts caller-supplied ``file://`` package URIs.
    Packages are published content-addressedly from a staging directory that
    the upload pipeline produced, and the Worker resolves them by fingerprint
    alone.
    """

    def __init__(self, exchange_root: Path):
        self.exchange_root = exchange_root.resolve()
        self.upload_root = (self.exchange_root / "uploads").resolve()
        self.package_root = (self.exchange_root / "packages").resolve()
        self.run_root = (self.exchange_root / "runs").resolve()

    # ------------------------------------------------------------------
    # Package publication / resolution
    # ------------------------------------------------------------------

    def package_dir(self, fingerprint: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("INVALID_PACKAGE_FINGERPRINT")
        return self.package_root / f"pkg_{fingerprint}.hepkg"

    def publish_package(self, staging: Path, fingerprint: str) -> PublishedPackage:
        """Atomically publish ``staging`` to ``pkg_<fingerprint>.hepkg``.

        The staging directory must live inside ``package_root`` (the upload
        pipeline guarantees this) so ``os.replace`` is a same-filesystem
        rename. If the target already exists it is re-validated and reused;
        a content mismatch raises ``PACKAGE_STATE_INCONSISTENT`` and is never
        overwritten.
        """
        target = self.package_dir(fingerprint)
        staging_resolved = staging.resolve()
        try:
            staging_resolved.relative_to(self.package_root)
        except ValueError as error:
            raise ValueError("PACKAGE_STAGING_OUTSIDE_ROOT") from error
        if target.exists():
            return self._reuse_package(staging_resolved, target, fingerprint)
        try:
            # Same-filesystem atomic rename. A concurrent publisher may win
            # between the exists() check and this operation.
            os_replace_dir(staging_resolved, target)
        except OSError:
            if not target.exists():
                raise
            return self._reuse_package(staging_resolved, target, fingerprint)
        return PublishedPackage(path=target, fingerprint=fingerprint, reused=False)

    def _reuse_package(
        self, staging: Path, target: Path, fingerprint: str
    ) -> PublishedPackage:
        """Re-validate an existing content-addressed Package before reuse."""
        from hyperextract.documents import document_package_fingerprint

        try:
            target_fingerprint = document_package_fingerprint(target)
        except (OSError, ValueError) as error:
            raise ValueError("PACKAGE_STATE_INCONSISTENT") from error
        if target_fingerprint != fingerprint:
            raise ValueError("PACKAGE_STATE_INCONSISTENT")
        shutil.rmtree(staging, ignore_errors=True)
        return PublishedPackage(path=target, fingerprint=fingerprint, reused=True)

    def resolve_package_ref(self, fingerprint: str) -> Path:
        """Resolve a content-addressed Package by its canonical fingerprint.

        Rejects ``.staging-*`` paths, escapes, missing packages, and symlink
        traversal. This replaces the old ``resolve_package_uri`` which
        accepted caller-supplied ``file://`` URIs.
        """
        target = self.package_dir(fingerprint)
        if _contains_symlink(target, self.package_root):
            raise ValueError("DOCUMENT_PACKAGE_PATH_FORBIDDEN")
        try:
            resolved = target.resolve(strict=True)
            resolved.relative_to(self.package_root)
        except (FileNotFoundError, RuntimeError, ValueError) as error:
            raise ValueError("DOCUMENT_PACKAGE_PATH_FORBIDDEN") from error
        if not resolved.is_dir():
            raise ValueError("DOCUMENT_PACKAGE_DIRECTORY_REQUIRED")
        if any(part.startswith(".staging") for part in resolved.parts):
            raise ValueError("DOCUMENT_PACKAGE_PATH_FORBIDDEN")
        return resolved

    # ------------------------------------------------------------------
    # Run directories
    # ------------------------------------------------------------------

    def output_locations(self, run_id: str) -> OutputLocations:
        return OutputLocations(
            run_id=run_id,
            self_link=f"/v1/runs/{run_id}",
            result_link=f"/v1/runs/{run_id}/result",
            result_metadata_link=f"/v1/runs/{run_id}/result-metadata",
            artifacts_link=f"/v1/runs/{run_id}/artifacts",
            errors_link=f"/v1/runs/{run_id}/errors",
        )

    def run_dir(self, run_id: str) -> Path:
        if not run_id.startswith("run_") or "/" in run_id or ".." in run_id:
            raise ValueError("INVALID_RUN_ID")
        return self.run_root / run_id

    def reserve_run(self, run_id: str) -> Path:
        target = self.run_dir(run_id)
        target.mkdir(parents=False, exist_ok=False)
        (target / "work").mkdir()
        (target / "state").mkdir()
        (target / "diagnostics" / "attempts").mkdir(parents=True)
        return target

    def discard_reserved_run(self, run_id: str) -> None:
        target = self.run_dir(run_id).resolve()
        target.relative_to(self.run_root)
        if (target / "artifacts").exists():
            raise ValueError("RUN_ALREADY_HAS_ARTIFACTS")
        if target.exists():
            shutil.rmtree(target)

    def progress_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "state" / "progress.json"

    def timeline_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "state" / "timeline.json"


def os_replace_dir(src: Path, dst: Path) -> None:
    """Atomic directory rename.

    ``os.replace`` works for directories on POSIX as long as ``dst`` does not
    exist or is empty. We rely on the caller having verified ``dst`` does not
    exist (content-addressed publication) or having reused it.
    """
    import os

    os.replace(src, dst)
