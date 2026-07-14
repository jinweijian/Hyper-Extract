import json
from types import SimpleNamespace

import pytest

from hyperextract.service.artifacts import ArtifactPublisher


def record(run_id, run_root):
    return SimpleNamespace(
        run_id=run_id,
        output_uri=(run_root / run_id).as_uri() + "/",
    )


def write_graph(work, run_id):
    graph = {
        "schema_name": "HyperExtractCourseGraph",
        "schema_version": "1.0",
        "run_id": run_id,
        "profile_version": "1",
        "outline_nodes": [
            {
                "id": "root",
                "name": "Course",
                "node_type": "book",
                "depth": 0,
                "parent_id": None,
                "order": 0,
                "source_refs": [],
            }
        ],
        "knowledge_nodes": [],
        "structural_edges": [],
        "semantic_edges": [],
    }
    (work / "course-graph.json").write_text(json.dumps(graph), encoding="utf-8")
    (work / "run-summary.json").write_text(json.dumps({"status": "completed"}))
    (work / "quality-report.json").write_text(json.dumps({"status": "passed"}))
    (work / "performance-report.json").write_text(
        json.dumps({"schema_name": "HyperExtractPerformanceReport"})
    )
    (work / "cost-report.json").write_text(
        json.dumps({"schema_name": "HyperExtractCostReport"})
    )


def test_publish_validates_hashes_and_writes_success_last(exchange_root):
    run_id = "run_artifact"
    work = exchange_root / "runs" / run_id / "work"
    work.mkdir(parents=True)
    write_graph(work, run_id)
    publisher = ArtifactPublisher(exchange_root / "runs")
    manifest = publisher.publish(record(run_id, exchange_root / "runs"), {})
    artifacts = exchange_root / "runs" / run_id / "artifacts"
    assert (artifacts / "_SUCCESS").exists()
    assert {item.name for item in manifest.artifacts} >= {
        "course_graph",
        "run_summary",
        "quality_report",
        "performance_report",
        "cost_report",
    }


def test_publish_refuses_partial_graph(exchange_root):
    run_id = "run_partial"
    (exchange_root / "runs" / run_id / "work").mkdir(parents=True)
    publisher = ArtifactPublisher(exchange_root / "runs")
    with pytest.raises(ValueError, match="course-graph"):
        publisher.publish(record(run_id, exchange_root / "runs"), {})
    assert not (exchange_root / "runs" / run_id / "artifacts/_SUCCESS").exists()


# ---------------------------------------------------------------------------
# Task 6: Reconcile published artifacts after crashes (inspect_published)
# ---------------------------------------------------------------------------


def _publish_valid(exchange_root, run_id):
    work = exchange_root / "runs" / run_id / "work"
    work.mkdir(parents=True)
    write_graph(work, run_id)
    publisher = ArtifactPublisher(exchange_root / "runs")
    publisher.publish(record(run_id, exchange_root / "runs"), {})
    return publisher


def test_inspect_published_returns_none_when_no_artifacts(exchange_root):
    publisher = ArtifactPublisher(exchange_root / "runs")
    assert publisher.inspect_published("run_no_artifacts") is None


def test_inspect_published_returns_manifest_when_valid(exchange_root):
    run_id = "run_inspect_valid"
    publisher = _publish_valid(exchange_root, run_id)
    manifest = publisher.inspect_published(run_id)
    assert manifest is not None
    assert manifest.run_id == run_id
    assert manifest.status == "completed"


def test_inspect_published_raises_when_only_marker_exists(exchange_root):
    run_id = "run_partial_marker"
    artifacts = exchange_root / "runs" / run_id / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "_SUCCESS").write_text("{}", encoding="utf-8")
    publisher = ArtifactPublisher(exchange_root / "runs")
    with pytest.raises(ValueError, match="ARTIFACT_STATE_INCONSISTENT"):
        publisher.inspect_published(run_id)


def test_inspect_published_raises_when_only_manifest_exists(exchange_root):
    run_id = "run_partial_manifest"
    artifacts = exchange_root / "runs" / run_id / "artifacts"
    artifacts.mkdir(parents=True)
    from hyperextract.service.artifacts import ArtifactManifest

    manifest = ArtifactManifest(run_id=run_id, artifacts=[])
    (artifacts / "artifact-manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    publisher = ArtifactPublisher(exchange_root / "runs")
    with pytest.raises(ValueError, match="ARTIFACT_STATE_INCONSISTENT"):
        publisher.inspect_published(run_id)


def test_inspect_published_raises_when_marker_manifest_hash_mismatch(exchange_root):
    run_id = "run_hash_mismatch"
    publisher = _publish_valid(exchange_root, run_id)
    marker_path = exchange_root / "runs" / run_id / "artifacts" / "_SUCCESS"
    marker_data = json.loads(marker_path.read_text())
    marker_data["manifest_sha256"] = "0" * 64
    marker_path.write_text(json.dumps(marker_data), encoding="utf-8")
    with pytest.raises(ValueError, match="ARTIFACT_STATE_INCONSISTENT"):
        publisher.inspect_published(run_id)


def test_inspect_published_raises_when_declared_artifact_missing(exchange_root):
    run_id = "run_missing_artifact"
    publisher = _publish_valid(exchange_root, run_id)
    (exchange_root / "runs" / run_id / "artifacts" / "course-graph.json").unlink()
    with pytest.raises(ValueError, match="ARTIFACT_STATE_INCONSISTENT"):
        publisher.inspect_published(run_id)


def test_inspect_published_raises_when_declared_artifact_corrupted(exchange_root):
    run_id = "run_corrupted_artifact"
    publisher = _publish_valid(exchange_root, run_id)
    path = exchange_root / "runs" / run_id / "artifacts" / "course-graph.json"
    path.write_text("not the original content", encoding="utf-8")
    with pytest.raises(ValueError, match="ARTIFACT_STATE_INCONSISTENT"):
        publisher.inspect_published(run_id)


def test_inspect_published_never_overwrites_partial_state(exchange_root):
    """A partial publication must fail and never be overwritten by re-publish."""
    run_id = "run_partial_never_overwrite"
    artifacts = exchange_root / "runs" / run_id / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "_SUCCESS").write_text("{}", encoding="utf-8")
    publisher = ArtifactPublisher(exchange_root / "runs")
    with pytest.raises(ValueError, match="ARTIFACT_STATE_INCONSISTENT"):
        publisher.inspect_published(run_id)
    # The partial state is still there — never overwritten
    assert (artifacts / "_SUCCESS").exists()
    assert not (artifacts / "artifact-manifest.json").exists()
