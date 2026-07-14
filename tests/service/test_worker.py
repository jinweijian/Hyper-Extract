import json
from pathlib import Path

from hyperextract.service.artifacts import ArtifactPublisher
from hyperextract.service.commands import RunCommand
from hyperextract.service.worker import ServiceWorker

from .test_artifacts import write_graph


class FakeExecutor:
    def execute(self, record):
        work = Path(record.output_uri.removeprefix("file://")) / "work"
        write_graph(work, record.run_id)
        return {"status": "completed"}


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
