import json

from hyperextract.documents import document_package_fingerprint
from tests.documents.test_document_package import _add_extraction_brief


def validate_request(client, package_path, version="1.0"):
    return client.post(
        "/v1/document-packages/validate",
        json={
            "contract_version": version,
            "package_uri": package_path.as_uri(),
            "sha256": document_package_fingerprint(package_path),
        },
    )


def test_contract_discovery_and_validation(client, package_path):
    contract = client.get("/v1/contracts/document-package/v1")
    assert contract.status_code == 200
    assert {item["path"] for item in contract.json()["required_entries"]} == {
        "manifest.json",
        "outline.json",
        "provenance.jsonl",
        "content/",
    }
    response = client.post(
        "/v1/document-packages/validate",
        json={
            "contract_version": "1.0",
            "package_uri": package_path.as_uri(),
            "sha256": document_package_fingerprint(package_path),
        },
    )
    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_contract_and_validation_expose_v1_1_extraction_brief(client, package_path):
    _add_extraction_brief(package_path)
    contract = client.get("/v1/contracts/document-package/v1").json()

    assert contract["schema_version"] == "1.1"
    assert contract["supported_versions"] == ["1.0", "1.1"]
    assert "extraction_brief" in contract["schemas"]
    assert contract["schemas"]["manifest"]["allOf"][0]["then"]["required"] == [
        "extraction_brief"
    ]

    response = client.post(
        "/v1/document-packages/validate",
        json={
            "contract_version": "1.1",
            "package_uri": package_path.as_uri(),
            "sha256": document_package_fingerprint(package_path),
        },
    )

    assert response.status_code == 200
    assert response.json()["schema_version"] == "1.1"
    assert response.json()["extraction_brief"]["id"] == "test-brief"


def test_validation_rejects_hash_before_queue(client, package_path):
    response = client.post(
        "/v1/document-packages/validate",
        json={
            "contract_version": "1.0",
            "package_uri": package_path.as_uri(),
            "sha256": "0" * 64,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DOCUMENT_PACKAGE_HASH_MISMATCH"


def test_validate_accepts_v1_1_only_when_manifest_matches(client, package_v1_1):
    response = client.post(
        "/v1/document-packages/validate",
        json={
            "contract_version": "1.1",
            "package_uri": package_v1_1.as_uri(),
            "sha256": document_package_fingerprint(package_v1_1),
        },
    )
    assert response.status_code == 200
    assert response.json()["schema_version"] == "1.1"


def test_validate_rejects_declared_version_mismatch(client, package_v1_1):
    response = client.post(
        "/v1/document-packages/validate",
        json={
            "contract_version": "1.0",
            "package_uri": package_v1_1.as_uri(),
            "sha256": document_package_fingerprint(package_v1_1),
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DOCUMENT_PACKAGE_VERSION_MISMATCH"


def test_service_contract_rejects_nonstandard_layout(client, package_v1_1):
    manifest = json.loads((package_v1_1 / "manifest.json").read_text())
    manifest["outline_path"] = "metadata/custom-outline.json"
    (package_v1_1 / "metadata").mkdir()
    (package_v1_1 / "outline.json").rename(
        package_v1_1 / "metadata" / "custom-outline.json"
    )
    (package_v1_1 / "manifest.json").write_text(json.dumps(manifest))

    response = validate_request(client, package_v1_1, version="1.1")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DOCUMENT_PACKAGE_LAYOUT_INVALID"
