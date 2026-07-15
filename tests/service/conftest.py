import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hyperextract.documents import document_package_fingerprint


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_package_archive(package_dir):
    """Return (archive_bytes, package_fingerprint, transport_sha256).

    The archive root directly contains the Document Package files (no nested
    top-level directory), matching the transport contract.
    """
    fingerprint = document_package_fingerprint(package_dir)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(Path(package_dir).rglob("*")):
            if path.is_file():
                tar.add(path, arcname=str(path.relative_to(package_dir)))
    archive = buffer.getvalue()
    transport = hashlib.sha256(archive).hexdigest()
    return archive, fingerprint, transport


def multipart_create_payload(package_dir, *, options=None, contract_version="1.1"):
    """Build (data, files) for a multipart POST /v1/runs request."""
    archive, fingerprint, transport = build_package_archive(package_dir)
    data = {
        "contract_version": contract_version,
        "package_fingerprint": fingerprint,
        "transport_sha256": transport,
    }
    if options is not None:
        data["options"] = json.dumps(options)
    files = {"package": ("course.hepkg.tar.gz", archive, "application/gzip")}
    return data, files


@pytest.fixture
def exchange_root(tmp_path):
    (tmp_path / "packages").mkdir()
    (tmp_path / "runs").mkdir()
    return tmp_path


@pytest.fixture
def package_path(exchange_root):
    root = exchange_root / "packages" / "course.hepkg"
    (root / "content").mkdir(parents=True)
    body = "## 2.1 Topic\n\nDefinition alpha.\n"
    (root / "content" / "body.md").write_text(body, encoding="utf-8")
    (root / "outline.json").write_text(
        json.dumps(
            {
                "schema_name": "HyperExtractOutline",
                "schema_version": "1.0",
                "nodes": [
                    {
                        "id": "root",
                        "title": "Course",
                        "depth": 0,
                        "parent_id": None,
                        "order": 0,
                        "source_refs": [],
                    },
                    {
                        "id": "section",
                        "title": "2.1 Topic",
                        "depth": 1,
                        "parent_id": "root",
                        "order": 1,
                        "source_refs": [{"ref": "source.md#L1-L3"}],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "provenance.jsonl").write_text(
        json.dumps(
            {"content_id": "body", "source_refs": [{"ref": "source.md#L1-L3"}]}
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_name": "HyperExtractDocumentPackage",
                "schema_version": "1.0",
                "document": {"id": "course", "title": "Course", "language": "en"},
                "producer": {"name": "test", "version": "1"},
                "outline_path": "outline.json",
                "provenance_path": "provenance.jsonl",
                "contents": [
                    {
                        "id": "body",
                        "path": "content/body.md",
                        "order": 0,
                        "content_kind": "body",
                        "outline_id": "section",
                        "sha256": _sha256(body),
                        "bytes": len(body.encode()),
                        "extract": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def package_v1_1(package_path):
    from tests.documents.test_document_package import _add_extraction_brief

    _add_extraction_brief(package_path)
    return package_path


@pytest.fixture
def settings(exchange_root):
    from hyperextract.service.settings import ServiceSettings

    return ServiceSettings(
        database_url="sqlite+pysqlite:///:memory:", exchange_root=exchange_root
    )


@pytest.fixture
def repository():
    from hyperextract.service.db import create_engine_and_session
    from hyperextract.service.repository import RunRepository

    engine, session_factory = create_engine_and_session(
        "sqlite+pysqlite:///:memory:", for_tests=True
    )
    yield RunRepository(session_factory)
    engine.dispose()


@pytest.fixture
def running_run(repository):
    from hyperextract.service.commands import RunCommand

    command = RunCommand(
        run_id="run_running",
        request_fingerprint="a" * 64,
        request_json={"input": {}},
        output_uri="/v1/runs/run_running",
        resolved_package_fingerprint="b" * 64,
    )
    repository.create_or_get(command, "running-key")
    return repository.claim_next("worker-1", lease_seconds=120)


@pytest.fixture
def cancellable_run(repository):
    """A running run owned by ``worker-1`` that can be cancelled mid-flight."""
    from hyperextract.service.commands import RunCommand

    command = RunCommand(
        run_id="run_cancellable",
        request_fingerprint="c" * 64,
        request_json={"input": {}},
        output_uri="/v1/runs/run_cancellable",
        resolved_package_fingerprint="b" * 64,
    )
    repository.create_or_get(command, "cancellable-key")
    return repository.claim_next("worker-1", lease_seconds=120)


@pytest.fixture
def expired_running_run(repository):
    """A running run whose lease has already expired (crashed worker scenario)."""
    from datetime import datetime, timedelta, timezone

    from hyperextract.service.commands import RunCommand
    from hyperextract.service.db_models import RunEntity

    command = RunCommand(
        run_id="run_expired",
        request_fingerprint="e" * 64,
        request_json={"input": {}},
        output_uri="/v1/runs/run_expired",
        resolved_package_fingerprint="b" * 64,
    )
    repository.create_or_get(command, "expired-key")
    repository.claim_next("worker-1", lease_seconds=120)
    with repository.session_factory.begin() as session:
        row = session.get(RunEntity, "run_expired")
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    return repository.get("run_expired")


@pytest.fixture
def worker(repository, settings):
    """A ServiceWorker with a fake executor that raises RunCancelled.

    Used by lifecycle tests in ``test_repository.py`` to verify that a
    cancellable running run ends up as ``cancelled`` after the worker
    processes it.
    """
    from hyperextract.documents.course_pipeline import RunCancelled
    from hyperextract.service.artifacts import ArtifactPublisher
    from hyperextract.service.worker import ServiceWorker

    class CancelExecutor:
        def execute(self, record):
            raise RunCancelled("Cancellation requested")

    return ServiceWorker(
        repository,
        CancelExecutor(),
        ArtifactPublisher(settings.run_root),
        settings,
        worker_id="worker-1",
    )


@pytest.fixture
def failed_run(repository, package_v1_1, settings):
    """Create a run through the API then mark it failed with attempt history."""
    from fastapi.testclient import TestClient

    from hyperextract.service.api.app import create_app
    from hyperextract.service.runtime import create_runtime

    class FakeProfiles:
        def validate(self, name, *, require_secrets=False, require_embedder=False, check_probe=False):
            if name != "openai-compatible-default":
                raise KeyError(name)

        def public_descriptor(self, name):
            if name != "openai-compatible-default":
                raise KeyError(name)
            return {"name": name, "fingerprint": "b" * 64}

    runtime = create_runtime(
        settings=settings,
        repository=repository,
        model_profiles=FakeProfiles(),
    )
    with TestClient(create_app(runtime=runtime)) as api_client:
        data, files = multipart_create_payload(package_v1_1, contract_version="1.1")
        response = api_client.post(
            "/v1/runs", headers={"Idempotency-Key": "failed"}, data=data, files=files
        )
        run_id = response.json()["run_id"]
        repository.fail(
            run_id,
            code="RUN_EXECUTION_FAILED",
            message="Extraction pipeline failed",
            resumable=False,
            source="worker",
        )
        from types import SimpleNamespace

        yield SimpleNamespace(run_id=run_id)


@pytest.fixture
def client(settings, repository):
    from hyperextract.service.api.app import create_app
    from hyperextract.service.runtime import create_runtime

    class FakeProfiles:
        def validate(self, name, *, require_secrets=False, require_embedder=False, check_probe=False):
            if name != "openai-compatible-default":
                raise KeyError(name)

        def public_descriptor(self, name):
            if name != "openai-compatible-default":
                raise KeyError(name)
            return {"name": name, "fingerprint": "b" * 64}

    runtime = create_runtime(
        settings=settings,
        repository=repository,
        model_profiles=FakeProfiles(),
    )
    with TestClient(create_app(runtime=runtime)) as value:
        yield value
