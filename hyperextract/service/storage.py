from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class OutputLocations:
    run_uri: str
    artifacts_uri: str
    manifest_uri: str
    success_marker_uri: str


def _contains_symlink(path: Path, stop: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == stop or current.parent == current:
            return False
        current = current.parent


class SharedVolumeStore:
    def __init__(self, exchange_root: Path):
        self.exchange_root = exchange_root.resolve()
        self.package_root = (self.exchange_root / "packages").resolve()
        self.run_root = (self.exchange_root / "runs").resolve()

    def resolve_package_uri(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise ValueError("DOCUMENT_PACKAGE_URI_MUST_USE_FILE")
        candidate = Path(unquote(parsed.path))
        if _contains_symlink(candidate, self.package_root):
            raise ValueError("DOCUMENT_PACKAGE_PATH_FORBIDDEN")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.package_root)
        except (FileNotFoundError, RuntimeError, ValueError) as error:
            raise ValueError("DOCUMENT_PACKAGE_PATH_FORBIDDEN") from error
        if not resolved.is_dir() or any(
            part.startswith(".staging") for part in resolved.parts
        ):
            raise ValueError("DOCUMENT_PACKAGE_DIRECTORY_REQUIRED")
        return resolved

    def output_locations(self, run_id: str) -> OutputLocations:
        run = self.run_root / run_id
        artifacts = run / "artifacts"
        return OutputLocations(
            run_uri=run.as_uri() + "/",
            artifacts_uri=artifacts.as_uri() + "/",
            manifest_uri=(artifacts / "artifact-manifest.json").as_uri(),
            success_marker_uri=(artifacts / "_SUCCESS").as_uri(),
        )

    def reserve_run(self, run_id: str) -> Path:
        if not run_id.startswith("run_") or "/" in run_id or ".." in run_id:
            raise ValueError("INVALID_RUN_ID")
        target = self.run_root / run_id
        target.mkdir(parents=False, exist_ok=False)
        (target / "work").mkdir()
        (target / "diagnostics" / "attempts").mkdir(parents=True)
        return target

    def discard_reserved_run(self, run_id: str) -> None:
        target = (self.run_root / run_id).resolve()
        target.relative_to(self.run_root)
        if (target / "artifacts").exists():
            raise ValueError("RUN_ALREADY_HAS_ARTIFACTS")
        if target.exists():
            shutil.rmtree(target)
