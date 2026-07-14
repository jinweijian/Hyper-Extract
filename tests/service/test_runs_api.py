from hyperextract.documents import document_package_fingerprint


def request_body(package_path, version="1.0"):
    return {
        "input": {
            "type": "document_package",
            "contract_version": version,
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


def test_create_get_cancel_and_idempotency(client, package_path):
    payload = request_body(package_path)
    first = client.post("/v1/runs", headers={"Idempotency-Key": "one"}, json=payload)
    assert first.status_code == 202
    body = first.json()
    assert body["status"] == "queued"
    assert body["output"]["manifest_uri"].endswith("artifact-manifest.json")
    duplicate = client.post(
        "/v1/runs", headers={"Idempotency-Key": "one"}, json=payload
    )
    assert duplicate.json()["run_id"] == body["run_id"]
    assert client.get(f"/v1/runs/{body['run_id']}").status_code == 200
    cancelled = client.post(f"/v1/runs/{body['run_id']}/cancel")
    assert cancelled.json()["status"] == "cancelled"


def test_create_rejects_changed_idempotent_request(client, package_path):
    payload = request_body(package_path)
    client.post("/v1/runs", headers={"Idempotency-Key": "same"}, json=payload)
    payload["execution"]["context_policy"] = "repack"
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "same"}, json=payload
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_create_rejects_unknown_model_profile_before_queue(client, package_path):
    payload = request_body(package_path)
    payload["execution"]["model_profile"] = "missing-profile"
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "missing"}, json=payload
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_PROFILE_INVALID"


def test_create_rejects_declared_version_mismatch(client, package_v1_1):
    payload = request_body(package_v1_1, version="1.0")
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "mismatch"}, json=payload
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DOCUMENT_PACKAGE_VERSION_MISMATCH"


def test_create_accepts_v1_1_package(client, package_v1_1):
    payload = request_body(package_v1_1, version="1.1")
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "v1-1"}, json=payload
    )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
