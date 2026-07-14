import json

import pytest

from hyperextract.documents.course_pipeline import (
    PipelineControl,
    PipelineOptions,
    RunCancelled,
    run_course_document,
)
from tests.documents.test_course_pipeline import FakeCourseGraph
from tests.documents.test_document_package import _write_package


def options():
    return PipelineOptions(
        target_tokens=100,
        max_tokens=200,
        max_workers=1,
        retry_attempts=1,
        heartbeat_interval=1,
        semantic_dedup=False,
        community_reports=False,
    )


def test_pipeline_injects_run_id_and_emits_events(tmp_path):
    events = []
    source = _write_package(tmp_path / "course.hepkg")
    result = run_course_document(
        source,
        tmp_path / "output",
        FakeCourseGraph(),
        options=options(),
        input_format="document-package",
        control=PipelineControl(
            run_id="run_service_1",
            event_sink=events.append,
            should_cancel=lambda: False,
        ),
    )
    assert result["run_id"] == "run_service_1"
    assert events
    graph = json.loads((tmp_path / "output/course-graph.json").read_text())
    assert graph["run_id"] == "run_service_1"


def test_pipeline_stops_before_model_call_when_cancelled(tmp_path):
    source = _write_package(tmp_path / "course.hepkg")
    graph = FakeCourseGraph()
    with pytest.raises(RunCancelled):
        run_course_document(
            source,
            tmp_path / "output",
            graph,
            options=options(),
            input_format="document-package",
            control=PipelineControl(should_cancel=lambda: True),
        )
    assert graph.extract_calls == 0
