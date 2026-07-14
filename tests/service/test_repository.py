import hashlib

import pytest

from hyperextract.service.commands import RunCommand
from hyperextract.service.repository import IdempotencyConflict


def command(run_id="run_1", request_fingerprint=None):
    return RunCommand(
        run_id=run_id,
        request_fingerprint=request_fingerprint or hashlib.sha256(b"request").hexdigest(),
        request_json={"input": {}},
        output_uri=f"file:///exchange/runs/{run_id}/",
    )


def test_create_or_get_is_idempotent(repository):
    first, created = repository.create_or_get(command(), "task-1")
    second, created_again = repository.create_or_get(command("run_2"), "task-1")
    assert created is True
    assert created_again is False
    assert second.run_id == first.run_id


def test_changed_idempotent_request_conflicts(repository):
    repository.create_or_get(command(), "task-1")
    with pytest.raises(IdempotencyConflict):
        repository.create_or_get(command("run_2", "f" * 64), "task-1")


def test_cancel_and_resume_state_machine(repository):
    run, _ = repository.create_or_get(command(), "task-1")
    assert repository.request_cancel(run.run_id).status == "cancelled"
    failed, _ = repository.create_or_get(command("run_2"), "task-2")
    repository.fail(failed.run_id, code="TRANSIENT", message="retry", resumable=True)
    resumed = repository.resume(failed.run_id)
    assert resumed.status == "queued"
    assert resumed.attempt == 2


def test_failure_is_recorded_and_queryable(repository, running_run):
    repository.fail(
        running_run.run_id,
        code="MODEL_RATE_LIMIT_EXHAUSTED",
        message="Provider request failed after retries",
        resumable=True,
    )
    errors = repository.list_errors(running_run.run_id)
    assert errors[0].code == "MODEL_RATE_LIMIT_EXHAUSTED"
    assert errors[0].attempt == 1
    assert errors[0].source == "worker"
    assert errors[0].message == "Provider request failed after retries"
    assert errors[0].occurred_at is not None


def test_list_errors_excludes_sensitive_details(repository, running_run):
    repository.fail(
        running_run.run_id,
        code="MODEL_RATE_LIMIT_EXHAUSTED",
        message="Provider request failed after retries",
        resumable=True,
        details={"api_key": "sk-leaked", "headers": {"Authorization": "Bearer x"}},
    )
    errors = repository.list_errors(running_run.run_id)
    assert errors[0].code == "MODEL_RATE_LIMIT_EXHAUSTED"
    # RunRecord exposes only safe fields, never details_json
    assert not hasattr(errors[0], "details")
    assert not hasattr(errors[0], "api_key")
    assert not hasattr(errors[0], "headers")
