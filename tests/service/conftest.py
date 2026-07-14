import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from hyperextract.documents import document_package_fingerprint


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
        output_uri="file:///exchange/runs/run_running/",
    )
    record, _ = repository.create_or_get(command, "running-key")
    return record


@pytest.fixture
def failed_run(repository, package_path, settings):
    """Create a run through the API then mark it failed with attempt history."""
    from fastapi.testclient import TestClient

    from hyperextract.service.api.app import create_app
    from hyperextract.service.runtime import create_runtime

    class FakeProfiles:
        def public_descriptor(self, name):
            if name != "minimax-course-default":
                raise KeyError(name)
            return {"name": name, "fingerprint": "b" * 64}

    runtime = create_runtime(
        settings=settings,
        repository=repository,
        model_profiles=FakeProfiles(),
    )
    with TestClient(create_app(runtime=runtime)) as api_client:
        payload = {
            "input": {
                "type": "document_package",
                "contract_version": "1.0",
                "package_uri": package_path.as_uri(),
                "package_format": "directory",
                "sha256": document_package_fingerprint(package_path),
            },
            "pipeline": {
                "name": "course_graph",
                "profile": {"name": "course_knowledge_graph", "version": "1"},
            },
            "execution": {"model_profile": "minimax-course-default"},
        }
        response = api_client.post(
            "/v1/runs", headers={"Idempotency-Key": "failed"}, json=payload
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
        def public_descriptor(self, name):
            if name != "minimax-course-default":
                raise KeyError(name)
            return {"name": name, "fingerprint": "b" * 64}

    runtime = create_runtime(
        settings=settings,
        repository=repository,
        model_profiles=FakeProfiles(),
    )
    with TestClient(create_app(runtime=runtime)) as value:
        yield value
