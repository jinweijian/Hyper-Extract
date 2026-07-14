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
