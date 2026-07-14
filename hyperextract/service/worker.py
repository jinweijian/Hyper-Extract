from __future__ import annotations

import inspect
import signal
import threading
import time
import uuid

from hyperextract.documents.course_pipeline import RunCancelled

from .artifacts import ArtifactPublisher
from .settings import ServiceSettings

_WORKER_VERSION = "1.0.0"


class ServiceWorker:
    def __init__(
        self,
        repository,
        executor,
        publisher: ArtifactPublisher,
        settings: ServiceSettings,
        *,
        worker_id: str,
    ):
        self.repository = repository
        self.executor = executor
        self.publisher = publisher
        self.settings = settings
        self.worker_id = worker_id

    def run_once(self) -> bool:
        # Recover any runs whose leases expired (e.g. a crashed worker)
        # before attempting to claim new work.
        self.repository.requeue_expired_leases(
            max_recoveries=self.settings.max_worker_recoveries
        )

        record = self.repository.claim_next(
            self.worker_id, lease_seconds=self.settings.lease_seconds
        )
        if record is None:
            # No work available — report an idle heartbeat so operators
            # can see the worker is alive.
            self.repository.heartbeat_worker(self.worker_id, _WORKER_VERSION)
            return False

        # If the run already has cancellation pending (re-claimed via
        # claim_next's cancel-requested path), finalize it immediately
        # without re-executing the pipeline.
        lease_lost = threading.Event()

        if record.cancel_requested:
            self.repository.mark_cancelled(record.run_id, self.worker_id)
            return True

        heartbeat_thread = self._start_heartbeat_thread(
            record.run_id, lease_lost
        )
        try:
            summary = self._execute(record, lease_lost)
        except RunCancelled:
            # If the lease was lost, another worker or requeue_expired_leases
            # has already taken over — do NOT call mark_cancelled or publish.
            if not lease_lost.is_set():
                self.repository.mark_cancelled(record.run_id, self.worker_id)
            return True
        except Exception as error:
            if not lease_lost.is_set():
                self.repository.fail(
                    record.run_id,
                    code="RUN_EXECUTION_FAILED",
                    message=f"{type(error).__name__}: {error}",
                    resumable=True,
                )
            return True
        else:
            # Only publish and complete if the lease is still ours.
            if lease_lost.is_set():
                return True
            self.publisher.publish(record, summary)
            self.repository.complete(record.run_id, summary)
            return True
        finally:
            lease_lost.set()  # signal the heartbeat thread to stop
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=5)

    def _execute(self, record, lease_lost):
        """Call the executor with the lease-lost signal.

        Executors that accept a ``lease_lost`` keyword argument can stop
        early when the lease is lost by checking the event in their
        control loop. The worker still checks ``lease_lost`` after
        execution and skips publishing if the lease was lost.
        """
        sig = inspect.signature(self.executor.execute)
        if "lease_lost" in sig.parameters:
            return self.executor.execute(record, lease_lost=lease_lost)
        return self.executor.execute(record)

    def _start_heartbeat_thread(
        self, run_id: str, lease_lost: threading.Event
    ) -> threading.Thread | None:
        """Start a daemon thread that renews the lease and reports a worker
        heartbeat every ``heartbeat_seconds``.

        If ``renew_lease`` returns False (the run is no longer owned by this
        worker), ``lease_lost`` is set so the executor can stop at the next
        control check and skip artifact publishing.
        """
        def _heartbeat():
            while not lease_lost.is_set():
                # Use Event.wait so we wake up immediately when lease_lost
                # is set (e.g. when execution finishes) instead of blocking
                # for the full heartbeat_seconds.
                if lease_lost.wait(timeout=self.settings.heartbeat_seconds):
                    return
                if not self.repository.renew_lease(
                    run_id, self.worker_id, self.settings.lease_seconds
                ):
                    lease_lost.set()
                    return
                self.repository.heartbeat_worker(self.worker_id, _WORKER_VERSION)

        thread = threading.Thread(target=_heartbeat, daemon=True)
        thread.start()
        return thread


def run_worker_loop(worker: ServiceWorker, settings: ServiceSettings) -> None:
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while not stopped:
        if not worker.run_once():
            time.sleep(settings.poll_seconds)


def main() -> None:
    from .runner import CourseRunExecutor
    from .runtime import create_runtime

    runtime = create_runtime()
    runtime.prepare()
    worker = ServiceWorker(
        runtime.repository,
        CourseRunExecutor(
            runtime.settings,
            runtime.repository,
            runtime.model_profiles,
        ),
        ArtifactPublisher(runtime.settings.run_root),
        runtime.settings,
        worker_id="worker-" + uuid.uuid4().hex[:12],
    )
    try:
        run_worker_loop(worker, runtime.settings)
    finally:
        runtime.close()
