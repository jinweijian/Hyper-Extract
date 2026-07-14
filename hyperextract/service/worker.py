from __future__ import annotations

import signal
import time
import uuid

from hyperextract.documents.course_pipeline import RunCancelled

from .artifacts import ArtifactPublisher
from .db import create_engine_and_session
from .repository import RunRepository
from .settings import ServiceSettings


class ServiceWorker:
    def __init__(
        self,
        repository: RunRepository,
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


def main() -> None:
    from .runner import CourseRunExecutor

    settings = ServiceSettings.from_env()
    engine, factory = create_engine_and_session(settings.database_url)
    repository = RunRepository(factory)
    worker = ServiceWorker(
        repository,
        CourseRunExecutor(settings, repository),
        ArtifactPublisher(settings.run_root),
        settings,
        worker_id="worker-" + uuid.uuid4().hex[:12],
    )
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while not stopped:
            if not worker.run_once():
                time.sleep(settings.poll_seconds)
    finally:
        engine.dispose()
