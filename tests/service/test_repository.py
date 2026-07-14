import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from hyperextract.service.commands import RunCommand
from hyperextract.service.repository import (
    IdempotencyConflict,
    InvalidRunState,
    LeaseOwnershipLost,
)


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
        worker_id="worker-1",
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
        worker_id="worker-1",
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


# ---------------------------------------------------------------------------
# Task 5: Complete cancellation, lease heartbeats, and crash recovery
# ---------------------------------------------------------------------------


def test_running_cancel_finishes_as_cancelled(worker, repository, cancellable_run):
    repository.request_cancel(cancellable_run.run_id)
    worker.run_once()
    assert repository.get(cancellable_run.run_id).status == "cancelled"


def test_expired_lease_requeues_same_run(repository, expired_running_run):
    recovered = repository.requeue_expired_leases(max_recoveries=3)
    record = repository.get(expired_running_run.run_id)
    assert recovered == [expired_running_run.run_id]
    assert record.status == "queued"
    assert record.resume_from_checkpoint is True


def test_active_lease_is_extended_independently_of_pipeline_events(
    repository, running_run
):
    before = repository.lease(running_run.run_id).lease_expires_at
    repository.renew_lease(running_run.run_id, "worker-1", lease_seconds=120)
    assert repository.lease(running_run.run_id).lease_expires_at > before


def test_expired_lease_cannot_be_renewed(repository, expired_running_run):
    assert (
        repository.renew_lease(
            expired_running_run.run_id,
            "worker-1",
            lease_seconds=120,
        )
        is False
    )


def test_stale_worker_cannot_mutate_run_after_new_worker_claims(repository):
    from hyperextract.service.db_models import RunEntity

    run, _ = repository.create_or_get(command("run_stale"), "stale-key")
    repository.claim_next("worker-old", lease_seconds=120)
    with repository.session_factory.begin() as session:
        row = session.get(RunEntity, run.run_id)
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    repository.requeue_expired_leases(max_recoveries=3)
    claimed = repository.claim_next("worker-new", lease_seconds=120)
    assert claimed.run_id == run.run_id
    assert claimed.lease_owner == "worker-new"

    with pytest.raises(LeaseOwnershipLost):
        repository.update_progress(
            run.run_id,
            "worker-old",
            stage="stale",
            progress={"source": "worker-old"},
        )
    with pytest.raises(LeaseOwnershipLost):
        repository.fail(
            run.run_id,
            worker_id="worker-old",
            code="STALE_FAILURE",
            message="must not win",
            resumable=True,
        )
    with pytest.raises(LeaseOwnershipLost):
        repository.complete(
            run.run_id,
            "worker-old",
            {"source": "worker-old"},
        )

    current = repository.get(run.run_id)
    assert current.status == "running"
    assert current.lease_owner == "worker-new"
    assert current.stage != "stale"


def test_completed_attempt_is_persisted(repository, running_run):
    repository.complete(
        running_run.run_id,
        "worker-1",
        {"status": "completed"},
    )

    attempts = repository.list_attempts(running_run.run_id)
    assert len(attempts) == 1
    assert attempts[0].attempt == 1
    assert attempts[0].status == "completed"
    assert attempts[0].started_at is not None
    assert attempts[0].ended_at is not None


def test_cancelled_attempt_is_persisted(repository, cancellable_run):
    repository.mark_cancelled(cancellable_run.run_id, "worker-1")

    attempts = repository.list_attempts(cancellable_run.run_id)
    assert len(attempts) == 1
    assert attempts[0].status == "cancelled"
    assert attempts[0].ended_at is not None


def test_mark_cancelled_verifies_lease_owner(repository, cancellable_run):
    with pytest.raises(InvalidRunState):
        repository.mark_cancelled(cancellable_run.run_id, "worker-other")
    # Original state is preserved — still running, still owned by worker-1
    record = repository.get(cancellable_run.run_id)
    assert record.status == "running"
    assert record.lease_owner == "worker-1"


def test_mark_cancelled_succeeds_for_owner(repository, cancellable_run):
    record = repository.mark_cancelled(cancellable_run.run_id, "worker-1")
    assert record.status == "cancelled"
    assert record.lease_owner is None


def test_mark_cancelled_rejects_non_running(repository):
    command = RunCommand(
        run_id="run_queued",
        request_fingerprint="a" * 64,
        request_json={},
        output_uri="file:///exchange/runs/run_queued/",
    )
    repository.create_or_get(command, "queued-key")
    with pytest.raises(InvalidRunState):
        repository.mark_cancelled("run_queued", "worker-1")


def test_renew_lease_returns_false_for_wrong_owner(repository, running_run):
    assert repository.renew_lease(running_run.run_id, "worker-other", lease_seconds=60) is False


def test_renew_lease_returns_false_for_non_running(repository):
    command = RunCommand(
        run_id="run_queued2",
        request_fingerprint="b" * 64,
        request_json={},
        output_uri="file:///exchange/runs/run_queued2/",
    )
    repository.create_or_get(command, "queued2-key")
    assert repository.renew_lease("run_queued2", "worker-1", lease_seconds=60) is False


def test_heartbeat_worker_creates_and_updates(repository):
    repository.heartbeat_worker("worker-1", "1.0.0")
    repository.heartbeat_worker("worker-1", "1.0.1")
    from hyperextract.service.db_models import WorkerHeartbeatEntity

    with repository.session_factory() as session:
        row = session.get(WorkerHeartbeatEntity, "worker-1")
        assert row is not None
        assert row.version == "1.0.1"
        assert row.last_seen_at is not None


def test_requeue_expired_leases_cancels_cancel_requested(repository):
    from hyperextract.service.db_models import RunEntity

    command = RunCommand(
        run_id="run_cancel_expired",
        request_fingerprint="c" * 64,
        request_json={},
        output_uri="file:///exchange/runs/run_cancel_expired/",
    )
    repository.create_or_get(command, "cancel-expired-key")
    repository.claim_next("worker-1", lease_seconds=120)
    with repository.session_factory.begin() as session:
        row = session.get(RunEntity, "run_cancel_expired")
        row.cancel_requested_at = datetime.now(timezone.utc)
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    recovered = repository.requeue_expired_leases(max_recoveries=3)
    record = repository.get("run_cancel_expired")
    assert recovered == []
    assert record.status == "cancelled"


def test_requeue_expired_leases_fails_when_recovery_exhausted(repository):
    from hyperextract.service.db_models import RunEntity

    command = RunCommand(
        run_id="run_exhausted",
        request_fingerprint="d" * 64,
        request_json={},
        output_uri="file:///exchange/runs/run_exhausted/",
    )
    repository.create_or_get(command, "exhausted-key")
    repository.claim_next("worker-1", lease_seconds=120)
    with repository.session_factory.begin() as session:
        row = session.get(RunEntity, "run_exhausted")
        row.recovery_count = 3
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    recovered = repository.requeue_expired_leases(max_recoveries=3)
    record = repository.get("run_exhausted")
    assert recovered == []
    assert record.status == "failed"
    assert record.resumable is True
    assert record.error_summary["code"] == "WORKER_RECOVERY_EXHAUSTED"
    errors = repository.list_errors("run_exhausted")
    assert len(errors) == 1
    assert errors[0].code == "WORKER_RECOVERY_EXHAUSTED"
    assert errors[0].source == "recovery"
    attempts = repository.list_attempts("run_exhausted")
    assert len(attempts) == 1
    assert attempts[0].status == "failed"
    assert attempts[0].ended_at is not None


def test_requeue_expired_leases_increments_recovery_count(repository):
    from hyperextract.service.db_models import RunEntity

    command = RunCommand(
        run_id="run_recoverable",
        request_fingerprint="e" * 64,
        request_json={},
        output_uri="file:///exchange/runs/run_recoverable/",
    )
    repository.create_or_get(command, "recoverable-key")
    repository.claim_next("worker-1", lease_seconds=120)
    with repository.session_factory.begin() as session:
        row = session.get(RunEntity, "run_recoverable")
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    recovered = repository.requeue_expired_leases(max_recoveries=3)
    record = repository.get("run_recoverable")
    assert recovered == ["run_recoverable"]
    assert record.status == "queued"
    assert record.resume_from_checkpoint is True
    # recovery_count should have been incremented from 0 to 1
    with repository.session_factory() as session:
        row = session.get(RunEntity, "run_recoverable")
        assert row.recovery_count == 1
