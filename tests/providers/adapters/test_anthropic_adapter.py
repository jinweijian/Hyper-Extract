from types import SimpleNamespace

import pytest

from hyperextract.providers.adapters.anthropic import AnthropicAdapter
from hyperextract.providers.contracts import (
    GenerationRequest,
    ModelCapabilities,
    ModelMessage,
    ProfileConfigurationError,
)


def test_anthropic_adapter_defensively_rejects_native_structured_mode():
    capabilities = ModelCapabilities(
        transport="anthropic_messages",
        structured_output_modes=["native", "text_json"],
        preferred_structured_output_mode="native",
        reasoning_content_mode="content_blocks",
        output_token_parameter="max_tokens",
        supported_parameters={"max_output_tokens", "timeout_seconds"},
        max_output_tokens=64,
    )
    adapter = AnthropicAdapter(
        model="claude-test",
        base_url=None,
        api_key="secret",
        capabilities=capabilities,
        client=SimpleNamespace(messages=SimpleNamespace(create=lambda **_: None)),
    )

    with pytest.raises(ProfileConfigurationError) as error:
        adapter.invoke(
            GenerationRequest(
                operation="test",
                messages=[ModelMessage(role="user", content="return json")],
                structured_output=True,
                structured_output_mode="native",
                output_schema={"type": "object"},
                request_id="anthropic-native",
            )
        )

    assert error.value.code == "STRUCTURED_OUTPUT_MODE_UNSUPPORTED"
