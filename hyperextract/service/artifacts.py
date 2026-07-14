from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hyperextract.documents.course_graph import CourseGraphV1


class ArtifactEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    path: str
    media_type: str
    schema_name: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    required: bool


class ArtifactManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    status: Literal["completed"] = "completed"
    artifacts: list[ArtifactEntry]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_sync(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


class ArtifactPublisher:
    REQUIRED = {
        "course-graph.json": ("course_graph", "HyperExtractCourseGraph"),
        "run-summary.json": ("run_summary", "HyperExtractRunSummary"),
        "quality-report.json": ("quality_report", "HyperExtractQualityReport"),
        "performance-report.json": (
            "performance_report",
            "HyperExtractPerformanceReport",
        ),
        "cost-report.json": ("cost_report", "HyperExtractCostReport"),
    }
    OPTIONAL = {
        "model-usage.json": ("model_usage", "HyperExtractModelUsage"),
        "course-evaluation.json": ("course_evaluation", "HyperExtractEvaluation"),
        "comparison-report.json": (
            "comparison_report",
            "HyperExtractCourseGraphComparison",
        ),
    }

    def __init__(self, run_root: Path):
        self.run_root = run_root.resolve()

    def publish(self, record, summary: dict) -> ArtifactManifest:
        run = (self.run_root / record.run_id).resolve()
        run.relative_to(self.run_root)
        work = run / "work"
        for filename in self.REQUIRED:
            if not (work / filename).is_file():
                raise ValueError(f"required artifact missing: {filename}")
        graph = CourseGraphV1.model_validate_json(
            (work / "course-graph.json").read_text(encoding="utf-8")
        )
        if graph.run_id != record.run_id:
            raise ValueError("course-graph run_id does not match the service run")

        staging = run / f".artifacts-{uuid.uuid4().hex}.tmp"
        staging.mkdir()
        entries: list[ArtifactEntry] = []
        try:
            declared = {**self.REQUIRED, **self.OPTIONAL}
            for filename, (name, schema_name) in declared.items():
                source = work / filename
                if not source.exists():
                    continue
                target = staging / filename
                shutil.copy2(source, target)
                entries.append(
                    ArtifactEntry(
                        name=name,
                        path=filename,
                        media_type="application/json",
                        schema_name=schema_name,
                        size=target.stat().st_size,
                        sha256=_sha256(target),
                        required=filename in self.REQUIRED,
                    )
                )
            manifest = ArtifactManifest(run_id=record.run_id, artifacts=entries)
            manifest_path = staging / "artifact-manifest.json"
            _write_sync(manifest_path, manifest.model_dump_json(indent=2))
            artifacts = run / "artifacts"
            if artifacts.exists():
                raise ValueError("artifacts already published")
            os.replace(staging, artifacts)
            marker = {
                "run_id": record.run_id,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "manifest": "artifact-manifest.json",
                "manifest_sha256": _sha256(artifacts / "artifact-manifest.json"),
            }
            _write_sync(artifacts / "_SUCCESS", json.dumps(marker, sort_keys=True))
            return manifest
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def inspect_published(self, run_id: str) -> ArtifactManifest | None:
        """Reconcile a previously published artifact set.

        Returns the validated :class:`ArtifactManifest` if a consistent
        publication exists for ``run_id``. Returns ``None`` when no
        publication has been attempted (neither ``_SUCCESS`` nor
        ``artifact-manifest.json`` exists). Raises
        ``ValueError("ARTIFACT_STATE_INCONSISTENT")`` when the publication
        is partial or fails hash/content verification — the caller must
        NEVER overwrite a partial publication.

        Correctness contract:
        * Both ``_SUCCESS`` and ``artifact-manifest.json`` must exist as files.
        * The ``manifest_sha256`` recorded in ``_SUCCESS`` must match the
          actual sha256 of ``artifact-manifest.json``.
        * Every artifact declared in the manifest must exist on disk with the
          declared size and sha256.
        """
        artifacts = self.run_root / run_id / "artifacts"
        marker = artifacts / "_SUCCESS"
        manifest_path = artifacts / "artifact-manifest.json"
        if not marker.exists() and not manifest_path.exists():
            return None
        if not marker.is_file() or not manifest_path.is_file():
            raise ValueError("ARTIFACT_STATE_INCONSISTENT")
        manifest = ArtifactManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        self._verify_marker_manifest_hash(marker, manifest_path)
        self._verify_every_declared_artifact(artifacts, manifest)
        return manifest

    def _verify_marker_manifest_hash(
        self, marker: Path, manifest_path: Path
    ) -> None:
        try:
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("ARTIFACT_STATE_INCONSISTENT") from error
        declared_sha = marker_data.get("manifest_sha256") if isinstance(
            marker_data, dict
        ) else None
        if not isinstance(declared_sha, str):
            raise ValueError("ARTIFACT_STATE_INCONSISTENT")
        actual_sha = _sha256(manifest_path)
        if declared_sha != actual_sha:
            raise ValueError("ARTIFACT_STATE_INCONSISTENT")

    def _verify_every_declared_artifact(
        self, artifacts: Path, manifest: ArtifactManifest
    ) -> None:
        for entry in manifest.artifacts:
            path = artifacts / entry.path
            if not path.is_file():
                raise ValueError("ARTIFACT_STATE_INCONSISTENT")
            if path.stat().st_size != entry.size:
                raise ValueError("ARTIFACT_STATE_INCONSISTENT")
            if _sha256(path) != entry.sha256:
                raise ValueError("ARTIFACT_STATE_INCONSISTENT")

    def save_attempt_diagnostics(
        self, run_id: str, attempt: int, *, error_type: str, message: str
    ) -> Path:
        """Persist detailed (redacted) diagnostics for a failed attempt.

        Diagnostics land under ``<run>/diagnostics/attempts/attempt-<N>.json``
        and never appear in the public API response — that surface only
        carries the ``message`` column from ``he_run_errors``.
        """
        diagnostics_dir = self.run_root / run_id / "diagnostics" / "attempts"
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": run_id,
            "attempt": attempt,
            "error_type": error_type,
            "error_message": message,
        }
        path = diagnostics_dir / f"attempt-{attempt}.json"
        _write_sync(path, json.dumps(payload, indent=2, sort_keys=True))
        return path
