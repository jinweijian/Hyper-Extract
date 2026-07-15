import json
from pathlib import Path

from hyperextract.service.timeline import (
    HE_TIMELINE_ACTIVITIES,
    TIMELINE_SCHEMA_VERSION,
    TIMELINE_STATUSES,
    current_step,
    fallback_timeline,
    new_timeline,
    overlay_current,
    prepare_timeline,
    read_timeline,
    transition,
    write_timeline,
)


def test_new_timeline_has_fixed_pending_plan():
    state = new_timeline("run_test", worker_id="worker-1", attempt=1)
    assert state.schema_version == TIMELINE_SCHEMA_VERSION
    assert tuple(step.activity for step in state.steps) == HE_TIMELINE_ACTIVITIES
    assert len(state.steps) == 9
    assert {step.status for step in state.steps} == {"pending"}


def test_lifecycle_transitions_do_not_add_steps():
    state = new_timeline("run_test", worker_id="worker-1", attempt=1)
    state = transition(
        state,
        activity="DOCUMENT_INGESTING",
        status="started",
        message="reading",
    )
    state = transition(
        state,
        activity="DOCUMENT_INGESTING",
        status="completed",
        message="read",
    )
    state = transition(
        state,
        activity="CHUNK_PLANNING",
        status="started",
        message="planning",
    )
    assert len(state.steps) == 9
    assert state.steps[0].status == "completed"
    assert state.steps[1].status == "running"
    assert sum(step.status == "running" for step in state.steps) == 1


def test_starting_later_stage_safely_closes_missing_predecessor_events():
    state = transition(
        new_timeline("run_test", worker_id="worker-1", attempt=1),
        activity="ARTIFACT_PUBLISHING",
        status="started",
        message="publishing",
    )

    assert all(step.status == "completed" for step in state.steps[:-1])
    assert all(step.completed_at is None for step in state.steps[:-1])
    assert state.steps[-1].status == "running"


def test_progress_overlay_only_changes_current_step():
    state = transition(
        new_timeline("run_test", worker_id="worker-1", attempt=1),
        activity="EXTRACTING_CHUNK",
        status="started",
    )
    state = overlay_current(
        state,
        activity="EXTRACTING_CHUNK",
        message="8/36",
        message_seq=31,
        current=8,
        total=36,
    )
    step = current_step(state)
    assert step is not None
    assert step.message == "8/36"
    assert step.message_seq == 31
    assert step.progress.current == 8
    assert len(state.steps) == 9


def test_prepare_recovery_preserves_completed_and_resets_incomplete():
    state = new_timeline("run_test", worker_id="old", attempt=1)
    state = transition(
        state, activity="DOCUMENT_INGESTING", status="completed"
    )
    state = transition(state, activity="CHUNK_PLANNING", status="started")
    recovered = prepare_timeline(
        state, run_id="run_test", worker_id="new", attempt=2
    )
    assert recovered.steps[0].status == "completed"
    assert recovered.steps[1].status == "pending"
    assert recovered.steps[1].started_at == state.steps[1].started_at
    assert recovered.attempt == 2
    assert recovered.worker_id == "new"


def test_timeline_atomic_roundtrip_and_corruption_fallback(tmp_path):
    path = tmp_path / "state" / "timeline.json"
    expected = new_timeline("run_test", worker_id="worker-1", attempt=1)
    write_timeline(path, expected)
    assert read_timeline(path) == expected

    path.write_text("{", encoding="utf-8")
    assert read_timeline(path) is None


def test_read_timeline_rejects_non_monotonic_lifecycle(tmp_path):
    path = tmp_path / "state" / "timeline.json"
    state = new_timeline("run_test", worker_id="worker-1", attempt=1)
    payload = state.to_dict()
    payload["steps"][2]["status"] = "running"
    payload["steps"][2]["attempt"] = 1
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert read_timeline(path) is None
    path.write_text(
        json.dumps({"schema_version": "999", "steps": []}), encoding="utf-8"
    )
    assert read_timeline(path) is None


def test_publish_database_fallback_is_explicit():
    state = fallback_timeline(
        run_id="run_test",
        attempt=1,
        status="running",
        db_stage="publish",
    )
    assert current_step(state).activity == "ARTIFACT_PUBLISHING"
    assert current_step(state).status == "running"
    assert all(step.status == "completed" for step in state.steps[:-1])
    assert all(step.started_at is None for step in state.steps)
    assert all(step.completed_at is None for step in state.steps)


def test_canonical_contract_fixtures_cover_lifecycle_cases():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "timeline-v1.fixtures.json").read_text(
            encoding="utf-8"
        )
    )
    assert fixture["schema_version"] == TIMELINE_SCHEMA_VERSION
    assert set(fixture["cases"]) == {
        "running",
        "completed",
        "failed",
        "recovered",
    }
    for case in fixture["cases"].values():
        steps = case["timeline"]
        assert tuple(step["activity"] for step in steps) == HE_TIMELINE_ACTIVITIES
        assert len(steps) == 9
        assert sum(step["status"] == "running" for step in steps) <= 1
        assert all(step["status"] in TIMELINE_STATUSES for step in steps)
