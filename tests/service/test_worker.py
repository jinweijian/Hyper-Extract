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
