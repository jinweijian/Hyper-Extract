import json
import threading
import time
from pathlib import Path

from hyperextract.documents.course_pipeline import RunCancelled
from hyperextract.service.artifacts import ArtifactPublisher
from hyperextract.service.commands import RunCommand
from hyperextract.service.worker import ServiceWorker

from .test_artifacts import write_graph


class FakeExecutor:
    def execute(self, record, *, lease_lost=None):
        work = Path(record.output_uri.removeprefix("file://")) / "work"
        write_graph(work, record.run_id)
        return {"status": "completed"}


def _make_command(run_id, output_uri):
    return RunCommand(
        run_id=run_id,
        request_fingerprint="a" * 64,
        request_json={},
        output_uri=output_uri,
    )


def test_worker_claims_executes_and_publishes(repository, settings):
    command = RunCommand(
        run_id="run_worker",
        request_fingerprint="a" * 64,
        request_json={},
        output_uri=(settings.run_root / "run_worker").as_uri() + "/",
    )
    repository.create_or_get(command, "worker-test")
    work = settings.run_root / "run_worker" / "work"
    work.mkdir(parents=True)
    worker = ServiceWorker(
        repository,
        FakeExecutor(),
        ArtifactPublisher(settings.run_root),
        settings,
        worker_id="worker-1",
    )
    assert worker.run_once() is True
    assert repository.get("run_worker").status == "completed"
    manifest = json.loads(
        (settings.run_root / "run_worker/artifacts/artifact-manifest.json").read_text()
    )
    assert manifest["status"] == "completed"


# ---------------------------------------------------------------------------
# Task 5: Cancellation, heartbeat, and lease-loss recovery
# ---------------------------------------------------------------------------


def test_run_cancelled_calls_mark_cancelled_not_request_cancel(repository, settings):
    """When the executor raises RunCancelled, the worker must call
    mark_cancelled (owner-checked), not request_cancel."""

    class CancelExecutor:
        def execute(self, record):
            raise RunCancelled("Pipeline cancellation requested")

    command = _make_command(
        "run_cancel_flow",
        (settings.run_root / "run_cancel_flow").as_uri() + "/",
    )
    repository.create_or_get(command, "cancel-flow-key")
    worker = ServiceWorker(
        repository,
        CancelExecutor(),
        ArtifactPublisher(settings.run_root),
        settings,
        worker_id="worker-1",
    )
    worker.run_once()
    record = repository.get("run_cancel_flow")
    assert record.status == "cancelled"
    assert record.lease_owner is None


def test_worker_does_not_publish_artifacts_on_cancellation(repository, settings):
    """Artifacts must not be published when a run is cancelled mid-flight."""

    class CancelExecutor:
        def execute(self, record):
            raise RunCancelled("Cancelled")

    command = _make_command(
        "run_no_publish_cancel",
        (settings.run_root / "run_no_publish_cancel").as_uri() + "/",
    )
    repository.create_or_get(command, "no-publish-key")
    worker = ServiceWorker(
        repository,
        CancelExecutor(),
        ArtifactPublisher(settings.run_root),
        settings,
        worker_id="worker-1",
    )
    worker.run_once()
    artifact_dir = settings.run_root / "run_no_publish_cancel/artifacts"
    assert not artifact_dir.exists()


def test_lease_loss_stops_execution_and_prevents_publishing(repository, settings):
    """If lease renewal returns False during execution, the executor must stop
    at the next control check and artifacts must NOT be published."""

    from dataclasses import replace

    from hyperextract.service.db_models import RunEntity

    class SlowExecutor:
        """Executor that polls ``lease_lost`` (passed by the worker) and
        raises ``RunCancelled`` when it is set."""

        def execute(self, record, *, lease_lost=None):
            work = Path(record.output_uri.removeprefix("file://")) / "work"
            work.mkdir(parents=True, exist_ok=True)
            for _ in range(200):
                if lease_lost is not None and lease_lost.is_set():
                    raise RunCancelled("Lease lost")
                time.sleep(0.01)
            write_graph(work, record.run_id)
            return {"status": "completed"}

    command = _make_command(
        "run_lease_loss",
        (settings.run_root / "run_lease_loss").as_uri() + "/",
    )
    repository.create_or_get(command, "lease-loss-key")
    # Use a very short heartbeat so the heartbeat thread detects lease loss
    # quickly during the test.
    test_settings = replace(settings, heartbeat_seconds=0.01)
    worker = ServiceWorker(
        repository,
        SlowExecutor(),
        ArtifactPublisher(settings.run_root),
        test_settings,
        worker_id="worker-1",
    )

    thread = threading.Thread(target=worker.run_once, daemon=True)
    thread.start()
    # Give the executor time to start, then make renew_lease return False
    # by changing the run's status away from "running".
    time.sleep(0.05)
    with repository.session_factory.begin() as session:
        row = session.get(RunEntity, "run_lease_loss")
        row.status = "queued"  # no longer running → renew_lease returns False
    thread.join(timeout=5)

    record = repository.get("run_lease_loss")
    # The run should NOT be completed by this worker.
    assert record.status != "completed"
    # Artifacts must NOT be published.
    artifact_dir = settings.run_root / "run_lease_loss/artifacts"
    assert not artifact_dir.exists()


def test_stale_worker_cannot_publish_after_run_is_reclaimed(repository, settings):
    """The database ownership check must fence a stale worker even when its
    heartbeat thread has not observed the lease transfer yet."""

    from datetime import datetime, timedelta, timezone

    from hyperextract.service.db_models import RunEntity

    run_id = "run_stale_publish"
    command = _make_command(
        run_id,
        (settings.run_root / run_id).as_uri() + "/",
    )
    repository.create_or_get(command, "stale-publish-key")
    work = settings.run_root / run_id / "work"
    work.mkdir(parents=True)

    class ReclaimingExecutor:
        def execute(self, record, *, lease_lost=None):
            with repository.session_factory.begin() as session:
                row = session.get(RunEntity, record.run_id)
                row.lease_expires_at = datetime.now(timezone.utc) - timedelta(
                    seconds=1
                )
            repository.requeue_expired_leases(max_recoveries=3)
            replacement = repository.claim_next("worker-new", lease_seconds=120)
            assert replacement.run_id == record.run_id
            write_graph(work, record.run_id)
            return {"status": "completed", "source": "worker-old"}

    worker = ServiceWorker(
        repository,
        ReclaimingExecutor(),
        ArtifactPublisher(settings.run_root),
        settings,
        worker_id="worker-old",
    )

    assert worker.run_once() is True
    current = repository.get(run_id)
    assert current.status == "running"
    assert current.lease_owner == "worker-new"
    assert not (settings.run_root / run_id / "artifacts").exists()


def test_worker_requeues_expired_leases_before_claiming(repository, settings):
    """The worker main loop should call requeue_expired_leases() before
    claiming new work."""

    from datetime import datetime, timedelta, timezone

    from hyperextract.service.db_models import RunEntity

    # Create an expired running run from a crashed worker.
    command = _make_command(
        "run_crashed",
        (settings.run_root / "run_crashed").as_uri() + "/",
    )
    repository.create_or_get(command, "crashed-key")
    repository.claim_next("worker-crashed", lease_seconds=120)
    with repository.session_factory.begin() as session:
        row = session.get(RunEntity, "run_crashed")
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=60)

    # Create work directories so FakeExecutor can succeed for whichever run
    # is claimed first.
    (settings.run_root / "run_crashed" / "work").mkdir(parents=True)
    (settings.run_root / "run_new" / "work").mkdir(parents=True)

    # Create a new queued run for the worker to pick up.
    command2 = _make_command(
        "run_new",
        (settings.run_root / "run_new").as_uri() + "/",
    )
    repository.create_or_get(command2, "new-key")

    worker = ServiceWorker(
        repository,
        FakeExecutor(),
        ArtifactPublisher(settings.run_root),
        settings,
        worker_id="worker-1",
    )
    worker.run_once()

    # The expired run should have been recovered (re-queued with
    # recovery_count incremented and resume_from_checkpoint set).
    crashed = repository.get("run_crashed")
    assert crashed.recovery_count == 1
    assert crashed.resume_from_checkpoint is True

    # The worker should have claimed and processed one of the queued runs.
    # (The crashed run is older, so it's claimed first.)
    assert crashed.status == "completed"


def test_worker_heartbeats_during_idle(repository, settings):
    """When no work is available, the worker should still report a heartbeat."""

    from hyperextract.service.db_models import WorkerHeartbeatEntity

    worker = ServiceWorker(
        repository,
        FakeExecutor(),
        ArtifactPublisher(settings.run_root),
        settings,
        worker_id="worker-idle",
    )
    worker.run_once()  # No work to claim
    with repository.session_factory() as session:
        row = session.get(WorkerHeartbeatEntity, "worker-idle")
        assert row is not None
        assert row.last_seen_at is not None


# ---------------------------------------------------------------------------
# Task 6: Reconcile published artifacts after crashes
# ---------------------------------------------------------------------------


def test_worker_reconciles_success_marker_without_rerunning_model(
    repository, settings
):
    """If artifacts are already published (crash after publish but before
    PostgreSQL ``complete``), the worker must complete the run in PostgreSQL
    WITHOUT rerunning the model pipeline."""

    from unittest.mock import MagicMock

    run_id = "run_reconcile_marker"
    command = _make_command(
        run_id, (settings.run_root / run_id).as_uri() + "/"
    )
    repository.create_or_get(command, "reconcile-marker-key")
    # Simulate a previous worker that published artifacts but crashed
    # before calling repository.complete().
    work = settings.run_root / run_id / "work"
    work.mkdir(parents=True)
    write_graph(work, run_id)
    publisher = ArtifactPublisher(settings.run_root)
    publisher.publish(repository.get(run_id), {"status": "completed"})

    executor = MagicMock()
    worker = ServiceWorker(
        repository,
        executor,
        publisher,
        settings,
        worker_id="worker-reconcile",
    )
    worker.run_once()

    assert repository.get(run_id).status == "completed"
    executor.execute.assert_not_called()


def test_worker_fails_run_when_publication_is_partial(repository, settings):
    """A partial publication (marker without manifest, or vice versa) must
    fail the run with ARTIFACT_STATE_INCONSISTENT and never be overwritten."""

    run_id = "run_partial_publication"
    command = _make_command(
        run_id, (settings.run_root / run_id).as_uri() + "/"
    )
    repository.create_or_get(command, "partial-publication-key")
    # Create a partial publication (marker only)
    artifacts = settings.run_root / run_id / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "_SUCCESS").write_text("{}", encoding="utf-8")

    from unittest.mock import MagicMock

    executor = MagicMock()
    worker = ServiceWorker(
        repository,
        executor,
        ArtifactPublisher(settings.run_root),
        settings,
        worker_id="worker-partial",
    )
    worker.run_once()

    record = repository.get(run_id)
    assert record.status == "failed"
    assert record.error_summary["code"] == "ARTIFACT_STATE_INCONSISTENT"
    # The executor must NOT have been called — partial publication is fatal.
    executor.execute.assert_not_called()
    # The partial state must not be overwritten.
    assert (artifacts / "_SUCCESS").exists()


def test_provider_secret_is_not_saved_in_failure(repository, settings):
    """Provider secrets (Bearer tokens, API keys) must not appear in the
    persisted failure message visible to API callers."""

    from unittest.mock import MagicMock

    run_id = "run_secret_redact"
    command = _make_command(
        run_id, (settings.run_root / run_id).as_uri() + "/"
    )
    repository.create_or_get(command, "secret-redact-key")
    (settings.run_root / run_id / "work").mkdir(parents=True)

    executor = MagicMock()
    executor.execute.side_effect = RuntimeError("Bearer sk-secret-value")
    worker = ServiceWorker(
        repository,
        executor,
        ArtifactPublisher(settings.run_root),
        settings,
        worker_id="worker-secret",
    )
    worker.run_once()

    errors = repository.list_errors(run_id)
    assert len(errors) == 1
    assert "sk-secret-value" not in errors[0].message
    # Public message is capped at 500 characters.
    assert len(errors[0].message) <= 500


def test_provider_api_key_pattern_is_redacted_in_failure(repository, settings):
    """The ``sk-`` API-key prefix pattern must be redacted from the
    persisted failure message."""

    from unittest.mock import MagicMock

    run_id = "run_api_key_redact"
    command = _make_command(
        run_id, (settings.run_root / run_id).as_uri() + "/"
    )
    repository.create_or_get(command, "api-key-redact-key")
    (settings.run_root / run_id / "work").mkdir(parents=True)

    executor = MagicMock()
    executor.execute.side_effect = RuntimeError(
        "Request failed with key=sk-AbcDef1234567890GhiJkl"
    )
    worker = ServiceWorker(
        repository,
        executor,
        ArtifactPublisher(settings.run_root),
        settings,
        worker_id="worker-api-key",
    )
    worker.run_once()

    error = repository.list_errors(run_id)[0]
    assert "sk-AbcDef1234567890GhiJkl" not in error.message


def test_worker_saves_attempt_diagnostics_under_diagnostics_dir(
    repository, settings
):
    """Detailed non-secret diagnostics must be saved under
    ``diagnostics/attempts/`` (NOT in the API response)."""

    from unittest.mock import MagicMock

    run_id = "run_diagnostics"
    command = _make_command(
        run_id, (settings.run_root / run_id).as_uri() + "/"
    )
    repository.create_or_get(command, "diagnostics-key")
    (settings.run_root / run_id / "work").mkdir(parents=True)

    executor = MagicMock()
    executor.execute.side_effect = RuntimeError("Bearer sk-secret-value")
    worker = ServiceWorker(
        repository,
        executor,
        ArtifactPublisher(settings.run_root),
        settings,
        worker_id="worker-diag",
    )
    worker.run_once()

    attempt_file = (
        settings.run_root / run_id / "diagnostics" / "attempts" / "attempt-1.json"
    )
    assert attempt_file.is_file()
    payload = json.loads(attempt_file.read_text())
    assert payload["error_type"] == "RuntimeError"
    # The diagnostics file is allowed to contain redacted secrets, but never
    # the raw secret value.
    assert "sk-secret-value" not in payload["error_message"]
