from fastapi.testclient import TestClient

from hyperextract.providers.contracts import ProfileConfigurationError
from hyperextract.service.api.app import create_app
from hyperextract.service.runtime import create_runtime

from .conftest import multipart_create_payload


def test_create_get_cancel_and_idempotency(client, package_v1_1):
    data, files = multipart_create_payload(package_v1_1)
    first = client.post(
        "/v1/runs", headers={"Idempotency-Key": "one"}, data=data, files=files
    )
    assert first.status_code == 202, first.text
    body = first.json()
    assert body["status"] == "queued"
    assert body["timeline_schema_version"] == "1.0"
    assert len(body["timeline"]) == 9
    assert {step["status"] for step in body["timeline"]} == {"pending"}
    assert first.headers["location"] == f"/v1/runs/{body['run_id']}"
    assert first.headers["retry-after"] == "2"
    duplicate = client.post(
        "/v1/runs", headers={"Idempotency-Key": "one"}, data=data, files=files
    )
    assert duplicate.json()["run_id"] == body["run_id"]
    assert client.get(f"/v1/runs/{body['run_id']}").status_code == 200
    cancelled = client.post(f"/v1/runs/{body['run_id']}/cancel")
    assert cancelled.json()["status"] == "cancelled"


def test_create_rejects_changed_idempotent_request(client, package_v1_1):
    data, files = multipart_create_payload(package_v1_1)
    client.post("/v1/runs", headers={"Idempotency-Key": "same"}, data=data, files=files)
    data2, files2 = multipart_create_payload(
        package_v1_1,
        options={"execution": {"context_policy": "repack"}},
    )
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "same"}, data=data2, files=files2
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_create_rejects_unknown_model_profile_before_queue(client, package_v1_1):
    data, files = multipart_create_payload(
        package_v1_1,
        options={"execution": {"model_profile": "missing-profile"}},
    )
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "missing"}, data=data, files=files
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_PROFILE_INVALID"


def test_create_rejects_required_profile_without_probe_before_queue(
    settings, repository, package_v1_1
):
    class ProbeRequiredProfiles:
        def validate(self, name, *, check_probe=False, **_kwargs):
            assert check_probe is True
            raise ProfileConfigurationError(
                "capability probe required",
                code="PROBE_REQUIRED",
            )

        def public_descriptor(self, name):
            raise AssertionError("descriptor must not be read after probe rejection")

    runtime = create_runtime(
        settings=settings,
        repository=repository,
        model_profiles=ProbeRequiredProfiles(),
    )
    with TestClient(create_app(runtime=runtime)) as api_client:
        data, files = multipart_create_payload(package_v1_1)
        response = api_client.post(
            "/v1/runs",
            headers={"Idempotency-Key": "probe-required"},
            data=data,
            files=files,
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_PROFILE_INVALID"


def test_create_rejects_declared_version_mismatch(client, package_v1_1):
    data, files = multipart_create_payload(package_v1_1, contract_version="1.0")
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "mismatch"}, data=data, files=files
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DOCUMENT_PACKAGE_VERSION_MISMATCH"


def test_create_accepts_v1_1_package(client, package_v1_1):
    data, files = multipart_create_payload(package_v1_1)
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "v1-1"}, data=data, files=files
    )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_errors_endpoint_returns_attempt_history(client, failed_run):
    response = client.get(f"/v1/runs/{failed_run.run_id}/errors")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == failed_run.run_id
    assert body["errors"][0]["code"] == "RUN_EXECUTION_FAILED"
    assert body["errors"][0]["attempt"] == 1
    assert body["errors"][0]["source"] == "worker"
    assert body["errors"][0]["message"] == "Extraction pipeline failed"
    assert "occurred_at" in body["errors"][0]
    # Redaction: response must never leak sensitive fields
    assert "details" not in body["errors"][0]
    assert "details_json" not in body["errors"][0]
    assert "headers" not in body["errors"][0]


def test_run_response_includes_errors_link(client, package_v1_1):
    data, files = multipart_create_payload(package_v1_1)
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "link-1"}, data=data, files=files
    )
    body = response.json()
    assert body["links"]["errors"] == f"/v1/runs/{body['run_id']}/errors"


def test_errors_endpoint_returns_404_for_unknown_run(client):
    response = client.get("/v1/runs/run_unknown/errors")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_run_response_does_not_leak_file_uris(client, package_v1_1):
    """The 202 response must not expose file:///exchange/... paths."""
    import json

    data, files = multipart_create_payload(package_v1_1)
    response = client.post(
        "/v1/runs", headers={"Idempotency-Key": "no-leak"}, data=data, files=files
    )
    assert response.status_code == 202
    serialized = json.dumps(response.json())
    assert "file:///exchange" not in serialized
    assert "/exchange/" not in serialized


def test_status_returns_dynamic_progress_for_running_run(
    client, package_v1_1, settings, repository
):
    """A running run with a valid progress snapshot returns dynamic progress."""
    from hyperextract.service import progress as progress_mod

    data, files = multipart_create_payload(package_v1_1)
    create = client.post(
        "/v1/runs", headers={"Idempotency-Key": "prog-1"}, data=data, files=files
    )
    run_id = create.json()["run_id"]
    # Claim the run as worker-1 so it is "running" with a lease owner.
    record = repository.claim_next("worker-1", lease_seconds=120)
    assert record.run_id == run_id
    # Write a valid progress snapshot owned by worker-1.
    snapshot = progress_mod.build_snapshot(
        run_id=run_id,
        attempt=record.attempt,
        worker_id="worker-1",
        sequence=37,
        stage="local_extract",
        activity="EXTRACTING_CHUNK",
        message="正在分析第 8/28 个内容块",
        current=8,
        total=28,
    )
    progress_mod.write_snapshot(settings.run_root / run_id / "state" / "progress.json", snapshot)

    response = client.get(f"/v1/runs/{run_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["status"] == "running"
    assert body["activity"] == "EXTRACTING_CHUNK"
    assert body["message"] == "正在分析第 8/28 个内容块"
    assert body["message_seq"] == 37
    assert body["progress"]["current"] == 8
    assert body["progress"]["total"] == 28
    assert body["progress"]["percent"] == 28.57
    assert len(body["timeline"]) == 9
    active = [step for step in body["timeline"] if step["status"] == "running"]
    assert len(active) == 1
    assert active[0]["activity"] == "EXTRACTING_CHUNK"
    assert active[0]["message_seq"] == 37
    assert active[0]["progress"]["current"] == 8


def test_status_degrades_when_progress_owner_mismatches(
    client, package_v1_1, settings, repository
):
    """A snapshot owned by a stale worker must not pollute the status."""
    from hyperextract.service import progress as progress_mod

    data, files = multipart_create_payload(package_v1_1)
    create = client.post(
        "/v1/runs", headers={"Idempotency-Key": "prog-2"}, data=data, files=files
    )
    run_id = create.json()["run_id"]
    record = repository.claim_next("worker-1", lease_seconds=120)
    # Stale snapshot owned by a different worker.
    snapshot = progress_mod.build_snapshot(
        run_id=run_id,
        attempt=record.attempt,
        worker_id="worker-stale",
        sequence=99,
        stage="local_extract",
        activity="EXTRACTING_CHUNK",
        message="stale message",
        current=99,
        total=100,
    )
    progress_mod.write_snapshot(settings.run_root / run_id / "state" / "progress.json", snapshot)

    body = client.get(f"/v1/runs/{run_id}").json()
    assert body["status"] == "running"
    # Stale data must NOT appear.
    assert body["message"] != "stale message"
    assert body["message_seq"] == 0
    assert body["progress"] is None
    assert len(body["timeline"]) == 9
    assert sum(step["status"] == "running" for step in body["timeline"]) == 1


def test_status_degrades_when_progress_file_missing(
    client, package_v1_1, repository
):
    """A running run with no progress file returns a safe fallback."""
    data, files = multipart_create_payload(package_v1_1)
    create = client.post(
        "/v1/runs", headers={"Idempotency-Key": "prog-3"}, data=data, files=files
    )
    run_id = create.json()["run_id"]
    repository.claim_next("worker-1", lease_seconds=120)
    body = client.get(f"/v1/runs/{run_id}").json()
    assert body["status"] == "running"
    assert body["progress"] is None
    assert body["message"]  # non-empty fallback
    assert body["activity"]


def test_status_for_completed_run_has_terminal_activity(client, package_v1_1, repository):
    data, files = multipart_create_payload(package_v1_1)
    create = client.post(
        "/v1/runs", headers={"Idempotency-Key": "prog-4"}, data=data, files=files
    )
    run_id = create.json()["run_id"]
    # Mark completed directly via repository (bypassing artifact publish).
    with repository.session_factory.begin() as session:
        from hyperextract.service.db_models import RunEntity

        row = session.get(RunEntity, run_id)
        row.status = "completed"
        row.stage = "completed"
        row.stage_status = "completed"
        row.lease_owner = None
        row.lease_expires_at = None
    body = client.get(f"/v1/runs/{run_id}").json()
    assert body["status"] == "completed"
    assert body["activity"] == "RUN_COMPLETED"
    assert all(step["status"] == "completed" for step in body["timeline"])
    assert body["progress"] is None


def test_status_rejects_stale_snapshot_after_worker_reclaim(
    client, package_v1_1, settings, repository
):
    """After a lease transfer, a snapshot written by the old worker must not
    pollute the status — even if the file still exists on disk."""
    from hyperextract.service import progress as progress_mod

    data, files = multipart_create_payload(package_v1_1)
    create = client.post(
        "/v1/runs", headers={"Idempotency-Key": "prog-reclaim"}, data=data, files=files
    )
    run_id = create.json()["run_id"]
    # Old worker claims and writes a snapshot.
    old = repository.claim_next("worker-old", lease_seconds=120)
    assert old.run_id == run_id
    old_snapshot = progress_mod.build_snapshot(
        run_id=run_id,
        attempt=old.attempt,
        worker_id="worker-old",
        sequence=10,
        stage="local_extract",
        activity="EXTRACTING_CHUNK",
        message="old worker message",
        current=5,
        total=10,
    )
    progress_mod.write_snapshot(settings.run_root / run_id / "state" / "progress.json", old_snapshot)
    # Simulate lease expiry + reclaim by a new worker (new attempt).
    from datetime import datetime, timedelta, timezone

    from hyperextract.service.db_models import RunEntity

    with repository.session_factory.begin() as session:
        row = session.get(RunEntity, run_id)
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    repository.requeue_expired_leases(max_recoveries=3)
    new = repository.claim_next("worker-new", lease_seconds=120)
    assert new.run_id == run_id
    assert new.lease_owner == "worker-new"
    # The old worker's snapshot is still on disk, but its worker_id
    # no longer matches the lease. The API must degrade.
    body = client.get(f"/v1/runs/{run_id}").json()
    assert body["status"] == "running"
    assert body["message"] != "old worker message"
    assert body["progress"] is None
