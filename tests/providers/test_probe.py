from datetime import datetime, timedelta, timezone

import pytest

from hyperextract.providers.contracts import (
    EmbeddingItemResult,
    EmbeddingResponse,
    GenerationResponse,
    ProbeResult,
    ProfileConfigurationError,
)
from hyperextract.providers.probe import (
    CapabilityProbe,
    ProbeStore,
    ensure_probe_eligibility,
)
from hyperextract.providers.profiles import (
    ModelProfile,
    ProfileCapabilities,
    ProfileEmbeddingCapabilities,
)


def _profile(required=False):
    return ModelProfile(
        name="production",
        llm="openai:model@https://example.test/v1",
        llm_api_key_env="TEST_KEY",
        probe_required=required,
        capabilities=ProfileCapabilities(),
    )


def test_probe_timestamp_does_not_change_profile_execution_identity(tmp_path):
    profile = _profile()
    before = profile.public_fingerprint()
    now = datetime.now(timezone.utc)
    ProbeStore(tmp_path).save(
        ProbeResult(
            profile_fingerprint=before,
            probe_evidence_hash="a" * 64,
            checks={"text": True},
            probed_at=now,
            expires_at=now + timedelta(hours=24),
        )
    )
    assert profile.public_fingerprint() == before


def test_enabling_probe_enforcement_keeps_existing_evidence_identity():
    optional = _profile(required=False)
    required = optional.model_copy(update={"probe_required": True})

    assert optional.public_fingerprint() == required.public_fingerprint()


def test_required_profile_rejects_missing_or_expired_probe(tmp_path):
    with pytest.raises(ProfileConfigurationError) as error:
        ensure_probe_eligibility(_profile(required=True), ProbeStore(tmp_path))
    assert error.value.code == "PROBE_REQUIRED"


def test_probe_store_uses_configured_persistent_root(monkeypatch, tmp_path):
    monkeypatch.setenv("HE_PROBE_ROOT", str(tmp_path))

    assert ProbeStore().root == tmp_path


def test_current_profile_ttl_can_expire_older_probe_early(tmp_path):
    profile = _profile(required=True).model_copy(update={"probe_ttl_hours": 1})
    probed_at = datetime.now(timezone.utc) - timedelta(hours=2)
    ProbeStore(tmp_path).save(
        ProbeResult(
            profile_fingerprint=profile.public_fingerprint(),
            probe_evidence_hash="b" * 64,
            checks={"text": True},
            probed_at=probed_at,
            expires_at=probed_at + timedelta(hours=24),
        )
    )

    with pytest.raises(ProfileConfigurationError) as error:
        ensure_probe_eligibility(profile, ProbeStore(tmp_path))

    assert error.value.code == "PROBE_REQUIRED"


class _ProbeGenerationAdapter:
    def __init__(self, *, fail_text=False):
        self.fail_text = fail_text
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        if request.operation == "probe.text":
            if self.fail_text:
                raise RuntimeError("provider unavailable")
            return GenerationResponse(
                request_id=request.request_id,
                final_text="OK",
                finish_reason="stop",
                input_tokens=2,
                output_tokens=1,
            )
        if request.operation == "probe.structured_list":
            text = '{"items":[{"value":"probe"}]}'
        else:
            text = '{"ok":true}'
        return GenerationResponse(
            request_id=request.request_id,
            final_text=text,
            finish_reason="stop",
            input_tokens=4,
            output_tokens=4,
        )


class _ProbeEmbeddingAdapter:
    def embed(self, request):
        items = []
        for index, value in enumerate(request.inputs):
            if not value:
                items.append(
                    EmbeddingItemResult(
                        input_index=index,
                        status="quarantined",
                        error_reason="empty_input",
                    )
                )
            else:
                items.append(
                    EmbeddingItemResult(
                        input_index=index,
                        status="completed",
                        vector=[1.0, 0.0],
                    )
                )
        return EmbeddingResponse(
            request_id=request.request_id,
            items=items,
            input_tokens=len(request.inputs),
        )


def test_probe_covers_generation_and_embedding_conformance_matrix():
    profile = ModelProfile(
        name="probe-matrix",
        llm="openai:model@https://example.test/v1",
        llm_api_key_env="TEST_KEY",
        embedder="openai:embedding@https://example.test/v1",
        embedder_api_key_env="EMBEDDING_KEY",
        capabilities=ProfileCapabilities(),
        embedding_capabilities=ProfileEmbeddingCapabilities(
            max_batch_items=2,
            max_input_tokens_per_item=4,
            empty_input_policy="quarantine",
        ),
    )

    result = CapabilityProbe(
        _ProbeGenerationAdapter(), _ProbeEmbeddingAdapter()
    ).run(profile)

    assert all(result.checks.values())
    assert {
        "structured_object",
        "structured_list",
        "thinking_separation",
        "declared_parameters",
        "embedding_string_inputs",
        "embedding_token_ids",
        "embedding_batch_limit",
        "embedding_long_input",
        "embedding_empty_policy",
        "embedding_alignment",
        "embedding_dimensions",
    } <= result.checks.keys()


def test_probe_converts_endpoint_exception_into_failed_evidence():
    result = CapabilityProbe(_ProbeGenerationAdapter(fail_text=True)).run(_profile())

    assert result.checks["text_response"] is False
    assert result.checks["declared_parameters"] is False
    assert result.observations["text_request_error"] == "RuntimeError"


def test_probe_store_invalidate_removes_stale_success(tmp_path):
    profile = _profile()
    store = ProbeStore(tmp_path)
    now = datetime.now(timezone.utc)
    store.save(
        ProbeResult(
            profile_fingerprint=profile.public_fingerprint(),
            probe_evidence_hash="c" * 64,
            checks={"text": True},
            probed_at=now,
            expires_at=now + timedelta(hours=24),
        )
    )

    store.invalidate(profile.public_fingerprint())

    assert store.load(profile.public_fingerprint()) is None
