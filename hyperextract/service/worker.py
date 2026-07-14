from __future__ import annotations

import signal
import time
import uuid

from hyperextract.documents.course_pipeline import RunCancelled

from .artifacts import ArtifactPublisher
from .settings import ServiceSettings


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
        record = self.repository.claim_next(
            self.worker_id, lease_seconds=self.settings.lease_seconds
        )
        if record is None:
            return False
        try:
            summary = self.executor.execute(record)
            self.publisher.publish(record, summary)
            self.repository.complete(record.run_id, summary)
        except RunCancelled:
            self.repository.request_cancel(record.run_id)
        except Exception as error:
            self.repository.fail(
                record.run_id,
                code="RUN_EXECUTION_FAILED",
                message=f"{type(error).__name__}: {error}",
                resumable=True,
            )
        return True


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
