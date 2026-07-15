import json
import shutil

from hyperextract.documents import document_package_fingerprint
from tests.documents.test_document_package import _add_extraction_brief


def _publish_package(store, package_path):
    """Copy package_path into a staging dir and publish by fingerprint."""
    fingerprint = document_package_fingerprint(package_path)
    import uuid

    staging = store.package_root / f".staging-{fingerprint[:8]}-{uuid.uuid4().hex[:6]}"
    staging.mkdir(parents=True)
    for item in package_path.iterdir():
        if item.is_dir():
            shutil.copytree(item, staging / item.name)
        else:
            shutil.copy2(item, staging / item.name)
    store.publish_package(staging, fingerprint)
    return fingerprint


def validate_request(client, store, package_path, version="1.1"):
    fingerprint = _publish_package(store, package_path)
    return client.post(
        "/v1/document-packages/validate",
        json={
            "contract_version": version,
            "package_fingerprint": fingerprint,
        },
    )


def test_contract_discovery_and_validation(client, settings, package_path):
    from hyperextract.service.storage import SharedVolumeStore

    store = SharedVolumeStore(settings.exchange_root)
    contract = client.get("/v1/contracts/document-package/v1")
    assert contract.status_code == 200
    assert {item["path"] for item in contract.json()["required_entries"]} == {
        "manifest.json",
        "outline.json",
        "provenance.jsonl",
        "content/",
    }
    fingerprint = _publish_package(store, package_path)
    response = client.post(
        "/v1/document-packages/validate",
        json={
            "contract_version": "1.0",
            "package_fingerprint": fingerprint,
        },
    )
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_capabilities_advertise_timeline_and_result_metadata(client):
    body = client.get("/v1/capabilities").json()
    assert body["timeline_schema_versions"] == ["1.0"]
    assert "result-metadata" in body["lifecycle"]
    assert body["contracts"] == {
        "run_status": "/v1/contracts/run-status/v1",
        "result_metadata": "/v1/contracts/result-metadata/v1",
    }


def test_run_status_contract_exposes_fixed_timeline_schema(client):
    response = client.get("/v1/contracts/run-status/v1")
    assert response.status_code == 200
    schema = response.json()
    timeline = schema["properties"]["timeline"]
    assert timeline["minItems"] == 9
    assert timeline["maxItems"] == 9
    assert schema["properties"]["timeline_schema_version"]["const"] == "1.0"
    step = schema["$defs"]["TimelineStepResponse"]
    assert step["properties"]["activity"]["enum"] == [
        "DOCUMENT_INGESTING",
        "CHUNK_PLANNING",
        "EXTRACTING_CHUNK",
        "DEDUPLICATING",
        "BUILDING_GLOBAL_EDGES",
        "QUALITY_CHECKING",
        "BUILDING_COMMUNITIES",
        "FINALIZING",
        "ARTIFACT_PUBLISHING",
    ]
    assert step["properties"]["status"]["enum"] == [
        "pending",
        "running",
        "completed",
        "failed",
        "skipped",
    ]


def test_result_metadata_contract_exposes_literal_identity(client):
    response = client.get("/v1/contracts/result-metadata/v1")
    assert response.status_code == 200
    schema = response.json()
    assert schema["properties"]["schema_name"]["const"] == (
        "HyperExtractResultMetadata"
    )
    assert schema["properties"]["schema_version"]["const"] == "1.0"


def test_contract_and_validation_expose_v1_1_extraction_brief(
    client, settings, package_path
):
    from hyperextract.service.storage import SharedVolumeStore

    _add_extraction_brief(package_path)
    contract = client.get("/v1/contracts/document-package/v1").json()

    assert contract["schema_version"] == "1.1"
    assert contract["supported_versions"] == ["1.0", "1.1"]
    assert "extraction_brief" in contract["schemas"]
    assert contract["schemas"]["manifest"]["allOf"][0]["then"]["required"] == [
        "extraction_brief"
    ]

    store = SharedVolumeStore(settings.exchange_root)
    fingerprint = _publish_package(store, package_path)
    response = client.post(
        "/v1/document-packages/validate",
        json={
            "contract_version": "1.1",
            "package_fingerprint": fingerprint,
        },
    )

    assert response.status_code == 200
    assert response.json()["schema_version"] == "1.1"
    assert response.json()["extraction_brief"]["id"] == "test-brief"


def test_validation_rejects_hash_before_queue(client, settings, package_path):
    from hyperextract.service.storage import SharedVolumeStore

    store = SharedVolumeStore(settings.exchange_root)
    _publish_package(store, package_path)
    response = client.post(
        "/v1/document-packages/validate",
        json={
            "contract_version": "1.0",
            "package_fingerprint": "0" * 64,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] in {
        "DOCUMENT_PACKAGE_HASH_MISMATCH",
        "DOCUMENT_PACKAGE_INVALID",
    }


def test_validate_accepts_v1_1_only_when_manifest_matches(
    client, settings, package_v1_1
):
    from hyperextract.service.storage import SharedVolumeStore

    store = SharedVolumeStore(settings.exchange_root)
    fingerprint = _publish_package(store, package_v1_1)
    response = client.post(
        "/v1/document-packages/validate",
        json={
            "contract_version": "1.1",
            "package_fingerprint": fingerprint,
        },
    )
    assert response.status_code == 200
    assert response.json()["schema_version"] == "1.1"


def test_validate_rejects_declared_version_mismatch(client, settings, package_v1_1):
    from hyperextract.service.storage import SharedVolumeStore

    store = SharedVolumeStore(settings.exchange_root)
    fingerprint = _publish_package(store, package_v1_1)
    response = client.post(
        "/v1/document-packages/validate",
        json={
            "contract_version": "1.0",
            "package_fingerprint": fingerprint,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DOCUMENT_PACKAGE_VERSION_MISMATCH"


def test_service_contract_rejects_nonstandard_layout(client, settings, package_v1_1):
    from hyperextract.service.storage import SharedVolumeStore

    manifest = json.loads((package_v1_1 / "manifest.json").read_text())
    manifest["outline_path"] = "metadata/custom-outline.json"
    (package_v1_1 / "metadata").mkdir()
    (package_v1_1 / "outline.json").rename(
        package_v1_1 / "metadata" / "custom-outline.json"
    )
    (package_v1_1 / "manifest.json").write_text(json.dumps(manifest))

    store = SharedVolumeStore(settings.exchange_root)
    response = validate_request(client, store, package_v1_1, version="1.1")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DOCUMENT_PACKAGE_LAYOUT_INVALID"
