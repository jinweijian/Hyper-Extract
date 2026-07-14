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
