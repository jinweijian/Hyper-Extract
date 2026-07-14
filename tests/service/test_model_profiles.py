import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hyperextract.documents import document_package_fingerprint
from hyperextract.service.model_profiles import ModelProfileRegistry


@pytest.fixture
def profile_file(tmp_path: Path) -> Path:
    path = tmp_path / "model-profiles.toml"
    path.write_text(
        """\
[profiles.minimax-course-default]
llm = "vllm:MiniMax-M2.7-highspeed@https://api.minimaxi.com/v1"
llm_api_key_env = "MIMIMAX_API_KEY"
embedder = "vllm:Qwen/Qwen3-Embedding-8B@https://api.siliconflow.cn/v1"
embedder_api_key_env = "EMBEDDING_API_KEY"
structured_output_mode = "text_json"
output_repair_attempts = 1
request_timeout = 900
""",
        encoding="utf-8",
    )
    return path


def test_public_descriptor_never_requires_or_contains_api_keys(
    profile_file, monkeypatch
):
    monkeypatch.delenv("MIMIMAX_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    registry = ModelProfileRegistry(profile_file)

    descriptor = registry.public_descriptor("minimax-course-default")

    assert descriptor["name"] == "minimax-course-default"
    assert "api_key" not in json.dumps(descriptor).lower()
    assert len(descriptor["fingerprint"]) == 64


def test_runtime_resolution_requires_worker_secrets(profile_file, monkeypatch):
    monkeypatch.delenv("MIMIMAX_API_KEY", raising=False)
    registry = ModelProfileRegistry(profile_file)

    with pytest.raises(ValueError, match="MODEL_PROFILE_ENV_MISSING"):
        registry.resolve_runtime("minimax-course-default")


def test_get_spec_returns_env_var_names_not_values(profile_file, monkeypatch):
    monkeypatch.setenv("MIMIMAX_API_KEY", "sk-secret-llm")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-secret-embed")
    registry = ModelProfileRegistry(profile_file)

    spec = registry.get_spec("minimax-course-default")

    assert spec.llm_api_key_env == "MIMIMAX_API_KEY"
    assert spec.embedder_api_key_env == "EMBEDDING_API_KEY"
    # Spec must never carry resolved secret values.
    dumped = json.dumps(spec.__dict__)
    assert "sk-secret-llm" not in dumped
    assert "sk-secret-embed" not in dumped


def test_fingerprint_is_stable_and_excludes_secrets(profile_file, monkeypatch):
    monkeypatch.delenv("MIMIMAX_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    registry = ModelProfileRegistry(profile_file)

    first = registry.public_descriptor("minimax-course-default")
    second = registry.public_descriptor("minimax-course-default")

    assert first["fingerprint"] == second["fingerprint"]
    assert len(first["fingerprint"]) == 64


def test_api_create_run_succeeds_without_model_secrets(
    profile_file, exchange_root, repository, package_path, monkeypatch
):
    """API must accept run creation with no model provider keys in env."""
    monkeypatch.delenv("MIMIMAX_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)

    from hyperextract.service.api.app import create_app
    from hyperextract.service.runtime import create_runtime
    from hyperextract.service.settings import ServiceSettings

    settings = ServiceSettings(
        database_url="sqlite+pysqlite:///:memory:",
        exchange_root=exchange_root,
        model_profiles_path=profile_file,
    )
    runtime = create_runtime(
        settings=settings,
        repository=repository,
        model_profiles=ModelProfileRegistry(profile_file),
    )

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

    with TestClient(create_app(runtime=runtime)) as client:
        response = client.post(
            "/v1/runs", headers={"Idempotency-Key": "no-secrets"}, json=payload
        )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
