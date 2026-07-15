import pytest

from hyperextract.providers.adapters.base import GenerationAdapterError
from hyperextract.providers.contracts import (
    CanonicalModelFailure,
    GenerationRequest,
    GenerationResponse,
    ModelMessage,
)
from hyperextract.providers.gateway import GatewayExecutionError, ModelExecutionGateway
from hyperextract.providers.profiles import (
    ModelProfile,
    ProfileCapabilities,
    ProfileRecovery,
)
from hyperextract.providers.scheduling import CircuitBreaker
from hyperextract.providers.scheduling import CircuitOpenError


class SequenceAdapter:
    name = "fake"

    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.requests = []

    def invoke(self, request):
        self.requests.append(request.model_copy(deep=True))
        value = self.sequence.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _error(category):
    failure = CanonicalModelFailure(request_id="r1", category=category, reason=category)
    return GenerationAdapterError(category, failure=failure)


def _profile():
    return ModelProfile(
        name="test",
        llm="openai:model@https://example.test/v1",
        llm_api_key_env="TEST_KEY",
        capabilities=ProfileCapabilities(
            structured_output_modes=["native", "text_json"],
            preferred_structured_output_mode="native",
            structured_output_fallback_order=["text_json"],
        ),
        recovery=ProfileRecovery(
            transient_retry_attempts=1,
            base_delay_seconds=0,
            max_delay_seconds=0,
        ),
    )


def _request():
    return GenerationRequest(
        operation="extract",
        messages=[ModelMessage(role="user", content="hi")],
        structured_output=True,
        request_id="r1",
    )


def test_gateway_falls_back_only_through_declared_modes():
    adapter = SequenceAdapter(
        [
            _error("unsupported_capability"),
            GenerationResponse(request_id="r1", final_text='{"ok":true}'),
        ]
    )
    gateway = ModelExecutionGateway(adapter, _profile(), sleep=lambda _: None)
    response = gateway.invoke(_request())
    assert response.final_text == '{"ok":true}'
    assert [request.structured_output_mode for request in adapter.requests] == [
        "native",
        "text_json",
    ]


def test_gateway_events_include_failure_chain_and_final_mode():
    events = []
    adapter = SequenceAdapter(
        [
            _error("unsupported_capability"),
            GenerationResponse(
                request_id="r1",
                final_text='{"ok":true}',
                provider_request_id="provider-1",
            ),
        ]
    )
    gateway = ModelExecutionGateway(
        adapter,
        _profile(),
        sleep=lambda _: None,
        event_sink=events.append,
    )

    gateway.invoke(_request())

    assert [event["status"] for event in events] == ["failed", "completed"]
    assert events[0]["decision"]["action"] == "fallback"
    assert events[0]["structured_output_mode"] == "native"
    assert events[1]["structured_output_mode"] == "text_json"
    assert events[1]["fallback_chain"] == ["native", "text_json"]
    assert events[1]["provider_request_id"] == "provider-1"


def test_circuit_rejection_is_emitted_without_provider_attempt():
    events = []
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    breaker.record_failure()
    adapter = SequenceAdapter([])
    gateway = ModelExecutionGateway(
        adapter,
        _profile(),
        circuit_breaker=breaker,
        event_sink=events.append,
    )

    with pytest.raises(GatewayExecutionError) as error:
        gateway.invoke(_request())

    assert isinstance(error.value.__cause__, CircuitOpenError)
    assert adapter.requests == []
    assert events[0]["status"] == "rejected"
    assert events[0]["decision"]["action"] == "circuit_break"
    assert gateway.last_trace.recoveries[0].decision.action == "circuit_break"


def test_gateway_transient_retry_keeps_identical_request_boundary():
    adapter = SequenceAdapter(
        [
            _error("transient"),
            GenerationResponse(request_id="r1", final_text="ok"),
        ]
    )
    gateway = ModelExecutionGateway(adapter, _profile(), sleep=lambda _: None)
    gateway.invoke(_request())
    assert adapter.requests[0] == adapter.requests[1]


def test_gateway_authentication_fails_without_retry():
    adapter = SequenceAdapter([_error("authentication")])
    gateway = ModelExecutionGateway(adapter, _profile(), sleep=lambda _: None)
    try:
        gateway.invoke(_request())
    except GatewayExecutionError as error:
        assert error.decision.target == "run"
    else:
        raise AssertionError("expected GatewayExecutionError")
    assert len(adapter.requests) == 1


def test_gateway_applies_profile_output_and_timeout_defaults():
    profile = _profile().model_copy(deep=True)
    profile.max_tokens = 321
    profile.request_timeout = 45
    adapter = SequenceAdapter(
        [GenerationResponse(request_id="r1", final_text='{"ok":true}')]
    )

    ModelExecutionGateway(adapter, profile).invoke(_request())

    assert adapter.requests[0].max_output_tokens == 321
    assert adapter.requests[0].timeout_seconds == 45


def test_gateway_auto_uses_profile_preferred_mode():
    profile = _profile().model_copy(deep=True)
    profile.capabilities.structured_output_modes = ["text_json"]
    profile.capabilities.preferred_structured_output_mode = "text_json"
    profile.capabilities.structured_output_fallback_order = []
    adapter = SequenceAdapter(
        [GenerationResponse(request_id="r1", final_text='{"ok":true}')]
    )
    request = _request().model_copy(update={"structured_output_mode": "auto"})

    ModelExecutionGateway(adapter, profile).invoke(request)

    assert adapter.requests[0].structured_output_mode == "text_json"


def test_half_open_non_transient_failure_releases_breaker():
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=1,
        cooldown_seconds=10,
        clock=lambda: now[0],
    )
    breaker.record_failure()
    now[0] = 11
    adapter = SequenceAdapter(
        [
            _error("authentication"),
            GenerationResponse(request_id="r1", final_text="ok"),
        ]
    )
    gateway = ModelExecutionGateway(
        adapter,
        _profile(),
        circuit_breaker=breaker,
        sleep=lambda _: None,
    )

    with pytest.raises(GatewayExecutionError):
        gateway.invoke(_request())

    assert breaker.state == "closed"
    assert gateway.invoke(_request()).final_text == "ok"
