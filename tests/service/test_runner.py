from types import SimpleNamespace
import time

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


def test_pipeline_options_use_configured_max_workers():
    settings = SimpleNamespace(
        heartbeat_seconds=30,
        pipeline_max_workers=6,
    )
    executor = CourseRunExecutor(
        settings,
        SimpleNamespace(),
        registry=SimpleNamespace(),
    )

    assert executor.pipeline_options(SimpleNamespace()).max_workers == 6


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
        pipeline_max_workers=2,
        progress_seconds=0.01,
        run_root=tmp_path,
        exchange_root=tmp_path,
        model_profiles_path=None,
    )
    # Publish a fake content-addressed package so resolve_package_ref succeeds.
    fp = "a" * 64
    pkg_dir = tmp_path / "packages" / f"pkg_{fp}.hepkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "manifest.json").write_text("{}", encoding="utf-8")
    record = SimpleNamespace(
        request_json={
            "execution": {"model_profile": profile.name},
            "resolved_package_ref": fp,
        },
        run_id="run-1",
        lease_owner="worker-1",
        attempt=1,
        resume_from_checkpoint=False,
    )
    captured = {}

    def graph_factory(llm, embedder, **kwargs):
        graph = SimpleNamespace()
        captured.update(llm=llm, embedder=embedder, kwargs=kwargs, graph=graph)
        return graph

    monkeypatch.setattr("hyperextract.service.runner.create_llm", lambda *a, **k: "llm")
    monkeypatch.setattr(
        "hyperextract.service.runner.document_package_fingerprint", lambda _: fp
    )
    monkeypatch.setattr(
        "hyperextract.service.runner.ensure_probe_eligibility", lambda _: None
    )
    monkeypatch.setattr(
        "hyperextract.service.runner.CourseKnowledgeGraph", graph_factory
    )

    def run_document(*args, **kwargs):
        captured["run_kwargs"] = kwargs
        kwargs["control"].event_sink(
            SimpleNamespace(
                stage="local_extract",
                status="started",
                message="开始知识抽取",
                current=0,
                total=10,
                timestamp="2026-07-15T00:00:00+00:00",
            )
        )
        kwargs["control"].event_sink(
            SimpleNamespace(
                stage="local_extract",
                status="progress",
                message="开始分析内容",
                current=1,
                total=10,
                timestamp="2026-07-15T00:00:01+00:00",
            )
        )
        time.sleep(0.04)
        kwargs["control"].event_sink(
            SimpleNamespace(
                stage="local_extract",
                status="completed",
                message="知识抽取完成",
                current=1,
                total=10,
                timestamp="2026-07-15T00:00:02+00:00",
            )
        )
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
    from hyperextract.service.progress import read_snapshot

    snapshot = read_snapshot(tmp_path / "run-1" / "state" / "progress.json")
    assert snapshot is not None
    assert snapshot.sequence >= 1
    assert snapshot.current == 1
    assert snapshot.total == 10
    from hyperextract.service.timeline import read_timeline

    lifecycle = read_timeline(tmp_path / "run-1" / "state" / "timeline.json")
    assert lifecycle is not None
    assert len(lifecycle.steps) == 9
    assert lifecycle.steps[2].status == "completed"
    # Ticker writes only progress.json; lifecycle sequence is exactly the two
    # stage boundary events even though several ticker intervals elapsed.
    assert lifecycle.sequence == 2

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


def test_service_runner_rejects_missing_package_ref(monkeypatch, tmp_path):
    """A run without resolved_package_ref must fail before model calls."""
    profile = ModelProfile(
        name="runner-no-ref",
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
    settings = SimpleNamespace(
        heartbeat_seconds=30,
        run_root=tmp_path,
        exchange_root=tmp_path,
        model_profiles_path=None,
    )
    record = SimpleNamespace(
        request_json={
            "execution": {"model_profile": profile.name},
            # resolved_package_ref intentionally omitted
        },
        run_id="run-no-ref",
        lease_owner="worker-1",
    )
    monkeypatch.setattr(
        "hyperextract.service.runner.ensure_probe_eligibility", lambda _: None
    )
    from hyperextract.providers.contracts import ProfileConfigurationError

    with pytest.raises(ProfileConfigurationError) as error:
        CourseRunExecutor(settings, SimpleNamespace(), registry).execute(record)
    assert error.value.code == "PACKAGE_REF_MISSING"


def test_service_runner_rejects_unresolvable_package_ref(monkeypatch, tmp_path):
    """A run whose package ref cannot be resolved must fail before model calls."""
    profile = ModelProfile(
        name="runner-bad-ref",
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
    settings = SimpleNamespace(
        heartbeat_seconds=30,
        run_root=tmp_path,
        exchange_root=tmp_path,
        model_profiles_path=None,
    )
    record = SimpleNamespace(
        request_json={
            "execution": {"model_profile": profile.name},
            "resolved_package_ref": "0" * 64,  # not published
        },
        run_id="run-bad-ref",
        lease_owner="worker-1",
    )
    monkeypatch.setattr(
        "hyperextract.service.runner.ensure_probe_eligibility", lambda _: None
    )
    from hyperextract.providers.contracts import ProfileConfigurationError

    with pytest.raises(ProfileConfigurationError) as error:
        CourseRunExecutor(settings, SimpleNamespace(), registry).execute(record)
    assert error.value.code == "PACKAGE_REF_INVALID"
