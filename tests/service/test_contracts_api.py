from hyperextract.documents import document_package_fingerprint


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
