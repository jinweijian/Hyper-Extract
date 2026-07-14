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


class OpenAIChatAdapter:
    """Adapter for OpenAI Chat Completions and compatible endpoints."""

    name = "openai_chat"
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
            from openai import OpenAI

            self._client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                max_retries=max_retries,
            )

    def invoke(self, request: GenerationRequest) -> GenerationResponse:
        kwargs = self._build_kwargs(request)
        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as error:
            failure = canonicalize_provider_error(
                error,
                request_id=request.request_id,
                secret_values=(self._api_key,),
            )
            raise GenerationAdapterError(
                str(failure.raw_message), failure=failure
            ) from error

        choices = getattr(response, "choices", None) or []
        if not choices:
            failure = canonicalize_provider_error(
                ValueError("provider response contains no choices"),
                request_id=request.request_id,
                category="protocol",
                reason="missing_choices",
                secret_values=(self._api_key,),
            )
            raise GenerationAdapterError(str(failure.raw_message), failure=failure)

        choice = choices[0]
        message = getattr(choice, "message", None)
        tool_text = _tool_arguments(message)
        normalized = (
            normalize_generation_payload(tool_text)
            if request.structured_output_mode == "tool" and tool_text is not None
            else normalize_generation_payload(
                message,
                reasoning_content_mode=self.capabilities.reasoning_content_mode,
            )
        )
        usage = getattr(response, "usage", None)
        return GenerationResponse(
            request_id=request.request_id,
            final_text=normalized.final_text,
            reasoning_text=normalized.reasoning_text,
            finish_reason=getattr(choice, "finish_reason", None),
            input_tokens=_usage_value(usage, "prompt_tokens", "input_tokens"),
            output_tokens=_usage_value(usage, "completion_tokens", "output_tokens"),
            provider_request_id=getattr(response, "id", None),
        )

    def _build_kwargs(self, request: GenerationRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [message.model_dump() for message in request.messages],
        }
        self._map_optional_parameter(
            kwargs,
            canonical="max_output_tokens",
            provider_name=self.capabilities.output_token_parameter,
            value=request.max_output_tokens,
        )
        self._map_optional_parameter(
            kwargs,
            canonical="temperature",
            provider_name="temperature",
            value=request.temperature,
        )
        self._map_optional_parameter(
            kwargs,
            canonical="timeout_seconds",
            provider_name="timeout",
            value=request.timeout_seconds,
        )

        if request.structured_output:
            mode = request.structured_output_mode or (
                self.capabilities.preferred_structured_output_mode
            )
            if mode not in self.capabilities.structured_output_modes:
                raise ProfileConfigurationError(
                    f"Structured output mode {mode!r} is not declared for {self.model!r}",
                    code="STRUCTURED_OUTPUT_MODE_UNSUPPORTED",
                )
            if mode == "native":
                if request.output_schema is None:
                    raise ProfileConfigurationError(
                        "native structured output requires output_schema",
                        code="OUTPUT_SCHEMA_REQUIRED",
                    )
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": _schema_name(request),
                        "strict": True,
                        "schema": request.output_schema,
                    },
                }
            elif mode == "json_object":
                kwargs["response_format"] = {"type": "json_object"}
            elif mode == "tool":
                if request.output_schema is None:
                    raise ProfileConfigurationError(
                        "tool structured output requires output_schema",
                        code="OUTPUT_SCHEMA_REQUIRED",
                    )
                kwargs["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": _schema_name(request),
                            "description": "Return the structured result",
                            "parameters": request.output_schema,
                        },
                    }
                ]
                kwargs["tool_choice"] = {
                    "type": "function",
                    "function": {"name": _schema_name(request)},
                }
            if mode in {"text_json", "json_object"} and request.output_schema:
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

    def _map_optional_parameter(
        self,
        kwargs: dict[str, Any],
        *,
        canonical: str,
        provider_name: str,
        value: Any,
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


def _usage_value(usage: Any, *names: str) -> int | None:
    if usage is None:
        return None
    for name in names:
        value = getattr(usage, name, None)
        if value is not None:
            return int(value)
    return None


def _schema_name(request: GenerationRequest) -> str:
    value = request.metadata.get("schema_name", "structured_output")
    safe = "".join(char if char.isalnum() or char == "_" else "_" for char in value)
    return safe[:64] or "structured_output"


def _tool_arguments(message: Any) -> str | None:
    calls = getattr(message, "tool_calls", None)
    if calls is None and isinstance(message, dict):
        calls = message.get("tool_calls")
    if not calls:
        return None
    first = calls[0]
    function = (
        first.get("function", {})
        if isinstance(first, dict)
        else getattr(first, "function", None)
    )
    if isinstance(function, dict):
        arguments = function.get("arguments")
    else:
        arguments = getattr(function, "arguments", None)
    return str(arguments) if arguments is not None else None
