import pytest
from fastapi.testclient import TestClient

from hyperextract.service.api.app import create_app
from hyperextract.service.commands import RunCommand
from hyperextract.service.runtime import create_runtime


@pytest.fixture
def fake_profiles():
    class FakeProfiles:
        def public_descriptor(self, name):
            if name != "openai-compatible-default":
                raise KeyError(name)
            return {"name": name, "fingerprint": "b" * 64}

    return FakeProfiles()


def test_api_routes_are_registered(settings, repository, fake_profiles):
    runtime = create_runtime(
        settings=settings,
        repository=repository,
        model_profiles=fake_profiles,
    )
    with TestClient(
        create_app(runtime=runtime)
    ) as client:
        paths = {route.path for route in client.app.routes}
    assert "/health/live" in paths
    assert "/v1/contracts/document-package/v1" in paths
    assert "/v1/runs" in paths


def test_run_command_is_not_owned_by_http_schemas():
    command = RunCommand(
        run_id="run_test",
        request_fingerprint="a" * 64,
        request_json={"input": {}},
        output_uri="file:///exchange/runs/run_test/",
    )
    assert command.run_id == "run_test"
