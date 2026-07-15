from types import SimpleNamespace

import pytest

from hyperextract.providers.langchain import AdapterEmbeddings
from hyperextract.providers.contracts import (
    EmbeddingItemResult,
    EmbeddingResponse,
)
from hyperextract.providers.profiles import (
    ModelProfile,
    ProfileEmbeddingCapabilities,
)
from hyperextract.providers.scheduling import PROCESS_CIRCUIT_BREAKERS
from hyperextract.service.model_profiles import ResolvedModelProfile
from hyperextract.service.runner import CourseRunExecutor


def test_service_runner_uses_adapters_gateway_and_shared_breaker(monkeypatch, tmp_path):
    profile = ModelProfile(
        name="runner-test",
        llm="openai:model@https://example.test/v1",
        llm_api_key_env="LLM_KEY",
        embedder="openai:embedding@https://example.test/v1",
        embedder_api_key_env="EMBEDDING_KEY",
        llm_rate_limit_group="shared-generation",
        embedder_rate_limit_group="shared-embedding",
        embedding_capabilities=ProfileEmbeddingCapabilities(),
    )
    generation_adapter = SimpleNamespace(name="generation")
    embedding_adapter = SimpleNamespace(name="embedding")
    providers = SimpleNamespace(
        create_generation_adapter=lambda _, **kwargs: generation_adapter,
        create_embedding_adapter=lambda _, **kwargs: embedding_adapter,
    )
    registry = SimpleNamespace(
        providers=providers,
        resolve_runtime=lambda _: ResolvedModelProfile(
            profile=profile,
            llm_api_key="llm-secret",
            embedder_api_key="embedding-secret",
        ),
    )
    settings = SimpleNamespace(
        heartbeat_seconds=30,
        run_root=tmp_path,
        model_profiles_path=None,
    )
    record = SimpleNamespace(
        request_json={
            "execution": {"model_profile": profile.name},
            "resolved_package_path": str(tmp_path / "package"),
        },
        run_id="run-1",
        lease_owner="worker-1",
    )
    captured = {}

    def graph_factory(llm, embedder, **kwargs):
        graph = SimpleNamespace()
        captured.update(llm=llm, embedder=embedder, kwargs=kwargs, graph=graph)
        return graph

    monkeypatch.setattr("hyperextract.service.runner.create_llm", lambda *a, **k: "llm")
    monkeypatch.setattr(
        "hyperextract.service.runner.ensure_probe_eligibility", lambda _: None
    )
    monkeypatch.setattr(
        "hyperextract.service.runner.CourseKnowledgeGraph", graph_factory
    )

    def run_document(*args, **kwargs):
        captured["run_kwargs"] = kwargs
        return {"status": "completed"}

    monkeypatch.setattr("hyperextract.service.runner.run_course_document", run_document)

    result = CourseRunExecutor(settings, SimpleNamespace(), registry).execute(record)

    assert result == {"status": "completed"}
    assert isinstance(captured["embedder"], AdapterEmbeddings)
    assert captured["embedder"].response_sink is not None
    gateway = captured["kwargs"]["generation_gateway"]
    assert gateway.adapter is generation_adapter
    assert gateway.circuit_breaker is PROCESS_CIRCUIT_BREAKERS.get("shared-generation")
    assert captured["kwargs"]["invalid_item_policy"] == "quarantine"
    assert captured["kwargs"]["invalid_item_ratio_threshold"] == 0.2
    assert captured["kwargs"]["validation_retry_attempts"] == 3
    assert gateway._event_sink is not None
    assert captured["run_kwargs"]["options"].recovery is profile.recovery

    captured["embedder"].response_sink(
        EmbeddingResponse(
            request_id="embedding-1",
            items=[
                EmbeddingItemResult(
                    input_index=0,
                    status="quarantined",
                    error_reason="invalid_input",
                )
            ],
        )
    )
    gateway._event_sink({"request_id": "generation-1"})
    checkpoint = tmp_path / "run-1" / "work" / ".he-run"
    assert (checkpoint / "embedding-rejections" / "embedding-1.json").is_file()
    assert (checkpoint / "diagnostics" / "model-gateway-events.jsonl").is_file()


def test_service_runner_rejects_profile_fingerprint_drift_before_model_calls(
    monkeypatch, tmp_path
):
    profile = ModelProfile(
        name="runner-fingerprint",
        llm="openai:model@https://example.test/v1",
        llm_api_key_env="LLM_KEY",
        embedder="openai:embedding@https://example.test/v1",
        embedder_api_key_env="EMBEDDING_KEY",
        embedding_capabilities=ProfileEmbeddingCapabilities(),
    )
    resolved = ResolvedModelProfile(
        profile=profile,
        llm_api_key="llm-secret",
        embedder_api_key="embedding-secret",
    )
    registry = SimpleNamespace(
        resolve_runtime=lambda _: resolved,
        providers=SimpleNamespace(),
    )
    record = SimpleNamespace(
        request_json={
            "execution": {"model_profile": profile.name},
            "resolved_config": {"model_profile_fingerprint": "stale"},
        },
        run_id="run-drift",
        lease_owner="worker-1",
    )
    settings = SimpleNamespace(
        heartbeat_seconds=30,
        run_root=tmp_path,
        model_profiles_path=None,
    )
    called = []
    monkeypatch.setattr(
        "hyperextract.service.runner.ensure_probe_eligibility",
        lambda _: called.append("probe"),
    )

    from hyperextract.providers.contracts import ProfileConfigurationError

    with pytest.raises(ProfileConfigurationError) as error:
        CourseRunExecutor(settings, SimpleNamespace(), registry).execute(record)

    assert error.value.code == "MODEL_PROFILE_FINGERPRINT_MISMATCH"
    assert called == []
