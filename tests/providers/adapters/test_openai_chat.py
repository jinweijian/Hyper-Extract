from types import SimpleNamespace

import pytest

from hyperextract.providers.adapters.openai_chat import OpenAIChatAdapter
from hyperextract.providers.adapters.base import GenerationAdapterError
from hyperextract.providers.contracts import (
    GenerationRequest,
    ModelCapabilities,
    ModelMessage,
    ProfileConfigurationError,
)


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _adapter(completions, **changes):
    values = {
        "transport": "openai_chat",
        "structured_output_modes": ["native", "tool", "json_object", "text_json"],
        "preferred_structured_output_mode": "text_json",
        "reasoning_content_mode": "separate_field",
        "output_token_parameter": "max_completion_tokens",
        "supported_parameters": {"max_output_tokens", "timeout_seconds"},
    }
    values.update(changes)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return OpenAIChatAdapter(
        model="model",
        base_url="https://example.test/v1",
        api_key="secret",
        capabilities=ModelCapabilities(**values),
        client=client,
    )


def _response(message):
    return SimpleNamespace(
        id="provider-id",
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
    )


def _request(**changes):
    values = {
        "operation": "extract",
        "messages": [ModelMessage(role="user", content="hi")],
        "request_id": "r1",
    }
    values.update(changes)
    return GenerationRequest(**values)


def test_maps_token_parameter_and_separates_reasoning():
    completions = FakeCompletions(
        _response(SimpleNamespace(content="answer", reasoning_content="hidden"))
    )
    response = _adapter(completions).invoke(_request(max_output_tokens=64))
    assert completions.calls[0]["max_completion_tokens"] == 64
    assert response.final_text == "answer"
    assert response.reasoning_text == "hidden"
    assert response.input_tokens == 3


def test_native_schema_is_sent_only_when_declared():
    completions = FakeCompletions(_response(SimpleNamespace(content='{"ok":true}')))
    _adapter(completions).invoke(
        _request(
            structured_output=True,
            structured_output_mode="native",
            output_schema={"type": "object"},
        )
    )
    assert completions.calls[0]["response_format"]["type"] == "json_schema"


def test_unsupported_optional_parameter_fails_before_call():
    completions = FakeCompletions(_response(SimpleNamespace(content="unused")))
    adapter = _adapter(completions)
    with pytest.raises(ProfileConfigurationError, match="temperature"):
        adapter.invoke(_request(temperature=0.5))
    assert completions.calls == []


def test_tool_arguments_become_final_json_text():
    message = SimpleNamespace(
        content=None,
        tool_calls=[SimpleNamespace(function=SimpleNamespace(arguments='{"ok":true}'))],
    )
    response = _adapter(FakeCompletions(_response(message))).invoke(
        _request(
            structured_output=True,
            structured_output_mode="tool",
            output_schema={"type": "object"},
        )
    )
    assert response.final_text == '{"ok":true}'


def test_missing_content_is_canonical_protocol_failure():
    message = SimpleNamespace(
        content=None,
        refusal="blocked",
        reasoning_content=None,
        additional_kwargs={},
    )

    with pytest.raises(GenerationAdapterError) as error:
        _adapter(FakeCompletions(_response(message))).invoke(_request())

    assert error.value.failure.category == "protocol"
    assert error.value.failure.reason == "content_filtered"
