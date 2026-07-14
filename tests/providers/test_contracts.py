from __future__ import annotations

import pytest
from pydantic import ValidationError

from hyperextract.providers.contracts import (
    CanonicalModelFailure,
    EmbeddingCapabilities,
    EmbeddingItemResult,
    EmbeddingRequest,
    EmbeddingResponse,
    GenerationRequest,
    GenerationResponse,
    ModelCapabilities,
    ModelMessage,
    ProfileConfigurationError,
    RecoveryDecision,
)


def test_model_message_constructs_from_valid_data():
    message = ModelMessage(role="user", content="hello")
    assert message.role == "user"
    assert message.content == "hello"


def test_model_message_rejects_unknown_role():
    with pytest.raises(ValidationError):
        ModelMessage(role="tool", content="hi")


def test_generation_request_constructs_from_valid_data():
    request = GenerationRequest(
        operation="extract",
        messages=[ModelMessage(role="user", content="hi")],
        request_id="r1",
    )
    assert request.operation == "extract"
    assert request.request_id == "r1"
    assert request.structured_output is False
    assert request.metadata == {}


def test_generation_response_constructs_from_valid_data():
    response = GenerationResponse(request_id="r1", final_text="done")
    assert response.request_id == "r1"
    assert response.final_text == "done"
    assert response.reasoning_text is None
    assert response.input_tokens is None


def test_embedding_request_constructs_from_valid_data():
    request = EmbeddingRequest(inputs=["a", "b"], request_id="r1")
    assert request.inputs == ["a", "b"]
    assert request.dimensions is None
    assert request.metadata == {}


def test_embedding_item_result_constructs_from_valid_data():
    item = EmbeddingItemResult(input_index=0, status="completed", vector=[0.1, 0.2])
    assert item.input_index == 0
    assert item.status == "completed"
    assert item.vector == [0.1, 0.2]
    assert item.error_reason is None


def test_embedding_item_result_quarantined_without_vector():
    item = EmbeddingItemResult(input_index=1, status="quarantined", error_reason="empty")
    assert item.vector is None
    assert item.error_reason == "empty"


def test_embedding_response_constructs_from_valid_data():
    response = EmbeddingResponse(
        request_id="r1",
        items=[EmbeddingItemResult(input_index=0, status="completed")],
    )
    assert response.request_id == "r1"
    assert len(response.items) == 1
    assert response.input_tokens is None


def test_model_capabilities_constructs_from_valid_data():
    caps = ModelCapabilities(
        transport="openai_chat",
        structured_output_modes=["text_json"],
        preferred_structured_output_mode="text_json",
        reasoning_content_mode="none",
        output_token_parameter="max_tokens",
        supported_parameters={"max_output_tokens", "timeout_seconds"},
    )
    assert caps.transport == "openai_chat"
    assert caps.recommended_concurrency == 1
    assert caps.context_tokens is None


def test_embedding_capabilities_constructs_with_defaults():
    caps = EmbeddingCapabilities(transport="openai_embeddings")
    assert caps.accepts_token_ids is False
    assert caps.supports_dimensions is False
    assert caps.empty_input_policy == "reject"


def test_canonical_model_failure_constructs_from_valid_data():
    failure = CanonicalModelFailure(
        request_id="r1",
        category="rate_limit.requests",
        reason="requests_per_minute",
        http_status=429,
        retry_after_seconds=1.5,
    )
    assert failure.category == "rate_limit.requests"
    assert failure.http_status == 429
    assert failure.raw_message is None


def test_profile_configuration_error_carries_code():
    error = ProfileConfigurationError("boom", code="SOMETHING_MISSING")
    assert isinstance(error, ValueError)
    assert error.code == "SOMETHING_MISSING"


LEGAL_RECOVERY_PAIRS = [
    ("retry", "request"),
    ("fallback", "request"),
    ("repair", "item"),
    ("repair", "batch"),
    ("split", "batch"),
    ("split", "chunk"),
    ("replan", "chunk"),
    ("quarantine", "item"),
    ("quarantine", "batch"),
    ("quarantine", "chunk"),
    ("fail", "request"),
    ("fail", "batch"),
    ("fail", "chunk"),
    ("fail", "run"),
    ("circuit_break", "rate_limit_group"),
]


ILLEGAL_RECOVERY_PAIRS = [
    ("split", "item"),
    ("circuit_break", "item"),
    ("retry", "batch"),
    ("fallback", "item"),
    ("repair", "request"),
    ("replan", "request"),
    ("quarantine", "request"),
    ("quarantine", "run"),
    ("fail", "item"),
    ("circuit_break", "batch"),
]


@pytest.mark.parametrize("action,target", LEGAL_RECOVERY_PAIRS)
def test_recovery_decision_accepts_legal_pairs(action, target):
    decision = RecoveryDecision(action=action, target=target, reason="ok")
    assert decision.action == action
    assert decision.target == target
    assert decision.delay_seconds == 0
    assert decision.consume_attempt is True


@pytest.mark.parametrize("action,target", ILLEGAL_RECOVERY_PAIRS)
def test_recovery_decision_rejects_illegal_pairs(action, target):
    with pytest.raises(ValidationError):
        RecoveryDecision(action=action, target=target, reason="bad")
