"""Tests for the main result download endpoint (Task 8)."""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from hyperextract.service.artifacts import ArtifactPublisher
from hyperextract.service.api.schemas.responses import ResultMetadataResponse

from .test_artifacts import write_graph


def _complete_run_with_artifacts(client, package_v1_1, settings, repository, key):
    from tests.service.conftest import multipart_create_payload

    data, files = multipart_create_payload(package_v1_1)
    create = client.post(
        "/v1/runs", headers={"Idempotency-Key": key}, data=data, files=files
    )
    run_id = create.json()["run_id"]
    # Publish artifacts directly.
    work = settings.run_root / run_id / "work"
    work.mkdir(parents=True, exist_ok=True)
    write_graph(work, run_id)
    (work / "run-summary.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "completed",
                "profile": {
                    "name": "course-knowledge-default",
                    "version": "1.1.0",
                    "content_hash": "a" * 64,
                    "prompt_hash": "b" * 64,
                },
                "extraction_brief": {
                    "id": "course-brief",
                    "version": "1.0",
                    "content_hash": "c" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    (work / "performance-report.json").write_text(
        json.dumps(
            {
                "schema_name": "HyperExtractPerformanceReport",
                "schema_version": "1.0",
                "wall_elapsed_seconds": 12.5,
                "chunks": 4,
            }
        ),
        encoding="utf-8",
    )
    (work / "quality-report.json").write_text(
        json.dumps(
            {
                "outline_sections": 10,
                "extractable_sections": 8,
                "covered_sections": 7,
                "directly_covered_sections": 6,
                "hierarchically_covered_sections": 7,
                "outline_coverage": 0.875,
                "uncovered_section_ids": ["section-8"],
                "knowledge_points": 20,
                "relations": 12,
                "relation_distribution": {
                    "prerequisite": 3,
                    "derivative": 3,
                    "related": 4,
                    "confusable": 2,
                },
                "dangling_edges": [],
                "passed": True,
            }
        ),
        encoding="utf-8",
    )
    publisher = ArtifactPublisher(settings.run_root)
    record = repository.get(run_id)
    publisher.publish(record, {"status": "completed"})
    # Mark the run completed in the DB.
    with repository.session_factory.begin() as session:
        from hyperextract.service.db_models import RunEntity

        row = session.get(RunEntity, run_id)
        row.status = "completed"
        row.stage = "completed"
        row.stage_status = "completed"
        row.lease_owner = None
        row.lease_expires_at = None
    return run_id


def test_result_streams_course_graph(client, package_v1_1, settings, repository):
    run_id = _complete_run_with_artifacts(
        client, package_v1_1, settings, repository, "result-1"
    )
    response = client.get(f"/v1/runs/{run_id}/result")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert "course-graph" in response.headers["content-disposition"]
    assert response.headers["content-length"]
    assert response.headers["etag"]
    body = json.loads(response.text)
    assert body["schema_name"] == "HyperExtractCourseGraph"
    assert body["run_id"] == run_id


def test_result_404_for_unknown_run(client):
    response = client.get("/v1/runs/run_unknown/result")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_result_409_when_not_completed(client, package_v1_1):
    from tests.service.conftest import multipart_create_payload

    data, files = multipart_create_payload(package_v1_1)
    create = client.post(
        "/v1/runs", headers={"Idempotency-Key": "result-409"}, data=data, files=files
    )
    run_id = create.json()["run_id"]
    response = client.get(f"/v1/runs/{run_id}/result")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ARTIFACTS_NOT_READY"


def test_result_500_when_artifacts_inconsistent(client, package_v1_1, settings, repository):
    run_id = _complete_run_with_artifacts(
        client, package_v1_1, settings, repository, "result-inconsistent"
    )
    # Corrupt the course graph so the manifest hash no longer matches.
    course = settings.run_root / run_id / "artifacts" / "course-graph.json"
    course.write_text("tampered", encoding="utf-8")
    response = client.get(f"/v1/runs/{run_id}/result")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "ARTIFACT_STATE_INCONSISTENT"


def test_result_endpoint_does_not_accept_caller_filename(client, package_v1_1, settings, repository):
    """The result endpoint must not accept caller-supplied filenames/paths."""
    run_id = _complete_run_with_artifacts(
        client, package_v1_1, settings, repository, "result-no-path"
    )
    # No query param accepted; route always returns the fixed course-graph.
    response = client.get(f"/v1/runs/{run_id}/result?file=model-audit/secret.json")
    # Query params are ignored by the route; still returns course-graph.
    assert response.status_code == 200
    body = json.loads(response.text)
    assert body["schema_name"] == "HyperExtractCourseGraph"


def test_result_metadata_returns_only_sanitized_validated_fields(
    client, package_v1_1, settings, repository
):
    run_id = _complete_run_with_artifacts(
        client, package_v1_1, settings, repository, "result-metadata"
    )
    response = client.get(f"/v1/runs/{run_id}/result-metadata")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_name"] == "HyperExtractResultMetadata"
    assert body["schema_version"] == "1.0"
    assert body["run_id"] == run_id
    assert body["profile"]["name"] == "course-knowledge-default"
    assert body["artifact"]["schema_name"] == "HyperExtractCourseGraph"
    assert body["artifact"]["size_bytes"] > 0
    assert body["performance"] == {"elapsed_seconds": 12.5, "chunk_count": 4}
    assert body["quality"]["dangling_edge_count"] == 0
    assert "path" not in json.dumps(body)
    assert "model_usage" not in json.dumps(body)


def test_result_metadata_requires_completed_run(client, package_v1_1):
    from tests.service.conftest import multipart_create_payload

    data, files = multipart_create_payload(package_v1_1)
    create = client.post(
        "/v1/runs",
        headers={"Idempotency-Key": "metadata-not-ready"},
        data=data,
        files=files,
    )
    response = client.get(
        f"/v1/runs/{create.json()['run_id']}/result-metadata"
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ARTIFACTS_NOT_READY"


def test_canonical_result_stream_and_metadata_fixtures_match():
    fixture_root = Path(__file__).parent / "fixtures"
    graph_bytes = (fixture_root / "course-graph-v1.fixture.json").read_bytes()
    graph = json.loads(graph_bytes)
    metadata = ResultMetadataResponse.model_validate_json(
        (fixture_root / "result-metadata-v1.fixture.json").read_text(
            encoding="utf-8"
        )
    )

    assert graph["schema_name"] == "HyperExtractCourseGraph"
    assert graph["run_id"] == metadata.run_id
    assert len(graph_bytes) == metadata.artifact.size_bytes
    assert sha256(graph_bytes).hexdigest() == metadata.artifact.sha256
