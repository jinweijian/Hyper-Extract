from __future__ import annotations

import json
from typing import Any

from hyperextract.providers.adapters.base import GenerationAdapterError
from hyperextract.providers.contracts import (
    GenerationRequest,
    GenerationResponse,
    ModelCapabilities,
    ProfileConfigurationError,
)
from hyperextract.providers.failures import canonicalize_provider_error
from hyperextract.providers.normalization import normalize_generation_payload

_INSTALL_HINT = "Install with: uv pip install 'hyperextract[anthropic]'"


class AnthropicAdapter:
    """Adapter for Anthropic's native Messages API."""

    name = "anthropic_messages"
    version = "1"

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None,
        api_key: str,
        capabilities: ModelCapabilities,
        client: Any | None = None,
        max_retries: int = 0,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self._api_key = api_key
        self.capabilities = capabilities
        if client is not None:
            self._client = client
        else:
            try:
                import anthropic
            except ImportError as error:
                raise ImportError(_INSTALL_HINT) from error
            self._client = anthropic.Anthropic(
                api_key=api_key,
                base_url=base_url,
                max_retries=max_retries,
            )

    def invoke(self, request: GenerationRequest) -> GenerationResponse:
        kwargs = self._build_kwargs(request)
        try:
            response = self._client.messages.create(**kwargs)
        except Exception as error:
            failure = canonicalize_provider_error(
                error,
                request_id=request.request_id,
                secret_values=(self._api_key,),
            )
            raise GenerationAdapterError(
                str(failure.raw_message), failure=failure
            ) from error
        content = getattr(response, "content", [])
        tool_text = _tool_input(content)
        normalized = (
            normalize_generation_payload(tool_text)
            if request.structured_output_mode == "tool" and tool_text is not None
            else normalize_generation_payload(
                content,
                reasoning_content_mode=self.capabilities.reasoning_content_mode,
            )
        )
        usage = getattr(response, "usage", None)
        return GenerationResponse(
            request_id=request.request_id,
            final_text=normalized.final_text,
            reasoning_text=normalized.reasoning_text,
            finish_reason=getattr(response, "stop_reason", None),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            provider_request_id=getattr(response, "id", None),
        )

    def _build_kwargs(self, request: GenerationRequest) -> dict[str, Any]:
        system = [
            message.content for message in request.messages if message.role == "system"
        ]
        messages = [
            message.model_dump()
            for message in request.messages
            if message.role != "system"
        ]
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if system:
            kwargs["system"] = "\n".join(system)
        maximum = request.max_output_tokens or self.capabilities.max_output_tokens
        if maximum is None:
            raise ProfileConfigurationError(
                "Anthropic Messages requires max_output_tokens",
                code="ANTHROPIC_MAX_TOKENS_REQUIRED",
            )
        self._map(
            kwargs,
            "max_output_tokens",
            self.capabilities.output_token_parameter,
            maximum,
        )
        self._map(kwargs, "temperature", "temperature", request.temperature)
        self._map(kwargs, "timeout_seconds", "timeout", request.timeout_seconds)
        if request.structured_output:
            mode = request.structured_output_mode or (
                self.capabilities.preferred_structured_output_mode
            )
            if mode not in self.capabilities.structured_output_modes:
                raise ProfileConfigurationError(
                    f"Structured output mode {mode!r} is not declared",
                    code="STRUCTURED_OUTPUT_MODE_UNSUPPORTED",
                )
            if mode == "tool":
                if request.output_schema is None:
                    raise ProfileConfigurationError(
                        "tool structured output requires output_schema",
                        code="OUTPUT_SCHEMA_REQUIRED",
                    )
                name = request.metadata.get("schema_name", "structured_output")
                kwargs["tools"] = [
                    {
                        "name": name,
                        "description": "Return the structured result",
                        "input_schema": request.output_schema,
                    }
                ]
                kwargs["tool_choice"] = {"type": "tool", "name": name}
            elif mode in {"text_json", "json_object"} and request.output_schema:
                kwargs["messages"].append(
                    {
                        "role": "user",
                        "content": (
                            "Return only JSON matching this schema; do not include "
                            "reasoning or Markdown fences:\n"
                            + json.dumps(request.output_schema, ensure_ascii=False)
                        ),
                    }
                )
        return kwargs

    def _map(
        self, kwargs: dict[str, Any], canonical: str, provider_name: str, value: Any
    ) -> None:
        if value is None:
            return
        if canonical not in self.capabilities.supported_parameters:
            if canonical in self.capabilities.omit_if_unsupported:
                return
            raise ProfileConfigurationError(
                f"Model {self.model!r} does not support parameter {canonical!r}",
                code="UNSUPPORTED_PARAMETER",
            )
        kwargs[provider_name] = value


def _tool_input(content: list[Any]) -> str | None:
    for block in content:
        kind = (
            block.get("type")
            if isinstance(block, dict)
            else getattr(block, "type", None)
        )
        if kind != "tool_use":
            continue
        value = (
            block.get("input")
            if isinstance(block, dict)
            else getattr(block, "input", None)
        )
        return json.dumps(value, ensure_ascii=False, default=str)
    return None
