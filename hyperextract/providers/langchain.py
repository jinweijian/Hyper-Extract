from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, PrivateAttr, TypeAdapter

from hyperextract.providers.adapters.base import AdapterError, EmbeddingAdapter
from hyperextract.providers.contracts import (
    EmbeddingRequest,
    EmbeddingResponse,
    GenerationRequest,
    ModelMessage,
)
from hyperextract.providers.gateway import ModelExecutionGateway
from hyperextract.providers.normalization import extract_json_value
from hyperextract.providers.scheduling import RateLimitGroupScheduler


class AdapterEmbeddings(Embeddings):
    """Expose the provider-neutral embedding contract through LangChain's API.

    Callers that can preserve partial success should use ``embed_with_status``.
    LangChain's vector-only API cannot represent a missing vector, so it raises
    ``EmbeddingQuarantineError`` instead of emitting a ragged/invalid matrix.
    """

    def __init__(
        self,
        adapter: EmbeddingAdapter,
        *,
        scheduler: RateLimitGroupScheduler | None = None,
        response_sink: Callable[[EmbeddingResponse], None] | None = None,
    ) -> None:
        self.adapter = adapter
        self.scheduler = (
            None if getattr(adapter, "_scheduler", None) is scheduler else scheduler
        )
        self.response_sink = response_sink
        self._local = threading.local()

    @property
    def last_response(self) -> EmbeddingResponse | None:
        return getattr(self._local, "last_response", None)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        response = self.embed_with_status(texts)
        quarantined = [
            item.input_index for item in response.items if item.vector is None
        ]
        if quarantined:
            raise EmbeddingQuarantineError(response)
        return [item.vector for item in response.items if item.vector is not None]

    def embed_with_status(self, texts: list[str]) -> EmbeddingResponse:
        request = EmbeddingRequest(inputs=texts, request_id=uuid.uuid4().hex)
        try:
            if self.scheduler is None:
                response = self.adapter.embed(request)
            else:
                with self.scheduler.slot(estimated_tokens=_estimate_tokens(texts)):
                    response = self.adapter.embed(request)
                self.scheduler.succeeded()
        except AdapterError as error:
            if self.scheduler is not None and error.failure.category.startswith(
                "rate_limit."
            ):
                self.scheduler.rate_limited(error.failure.retry_after_seconds or 1.0)
            raise
        self._local.last_response = response
        if self.response_sink is not None:
            self.response_sink(response)
        return response

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def _estimate_tokens(texts: list[str]) -> int:
    return max(1, sum(len(text) for text in texts) // 4)


class EmbeddingQuarantineError(RuntimeError):
    """LangChain vector APIs cannot represent quarantined embedding positions."""

    def __init__(self, response: EmbeddingResponse) -> None:
        self.response = response
        indices = [item.input_index for item in response.items if item.vector is None]
        super().__init__(f"Embedding inputs were quarantined at indices {indices}")


class AdapterChatModel(BaseChatModel):
    """Expose ``ModelExecutionGateway`` through LangChain's chat model API."""

    model_name: str
    temperature: float | None = None
    max_output_tokens: int | None = None
    timeout_seconds: int | None = None
    _gateway: ModelExecutionGateway = PrivateAttr()

    def __init__(self, gateway: ModelExecutionGateway, **data) -> None:
        super().__init__(**data)
        self._gateway = gateway

    @property
    def model_execution_gateway(self) -> ModelExecutionGateway:
        return self._gateway

    @property
    def _llm_type(self) -> str:
        return "hyperextract_adapter_gateway"

    @property
    def _identifying_params(self) -> dict[str, object]:
        return {
            "model_name": self.model_name,
            "profile": self._gateway.profile.name,
            "profile_fingerprint": self._gateway.profile.public_fingerprint(),
        }

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        del run_manager
        if stop:
            raise ValueError("Profile-backed AdapterChatModel does not support stop")
        response = self._gateway.invoke(
            GenerationRequest(
                operation=str(kwargs.pop("operation", "langchain.invoke")),
                messages=[_to_model_message(message) for message in messages],
                max_output_tokens=kwargs.pop(
                    "max_output_tokens", self.max_output_tokens
                ),
                temperature=kwargs.pop("temperature", self.temperature),
                timeout_seconds=kwargs.pop("timeout_seconds", self.timeout_seconds),
                request_id=str(kwargs.pop("request_id", uuid.uuid4().hex)),
                metadata={"langchain_model": self.model_name},
            )
        )
        metadata = {
            "finish_reason": response.finish_reason,
            "provider_request_id": response.provider_request_id,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
        }
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=response.final_text,
                        response_metadata={
                            key: value
                            for key, value in metadata.items()
                            if value is not None
                        },
                    )
                )
            ]
        )

    def with_structured_output(
        self,
        schema: dict | type,
        *,
        include_raw: bool = False,
        **kwargs,
    ) -> RunnableLambda:
        method = kwargs.pop("method", None)
        if kwargs:
            raise ValueError(
                f"Unsupported structured-output options: {', '.join(sorted(kwargs))}"
            )
        mode_by_method = {
            "json_schema": "native",
            "function_calling": "tool",
            "json_mode": "json_object",
        }
        requested_mode = mode_by_method.get(method)
        declared_modes = self._gateway.profile.capabilities.structured_output_modes
        mode = (
            requested_mode
            if requested_mode in declared_modes
            else self._gateway.profile.capabilities.preferred_structured_output_mode
        )
        json_schema = _schema_json(schema)

        def invoke_structured(value):
            prompt = self._convert_input(value)
            response = self._gateway.invoke(
                GenerationRequest(
                    operation="langchain.structured_output",
                    messages=[
                        _to_model_message(message) for message in prompt.to_messages()
                    ],
                    output_schema=json_schema,
                    structured_output=True,
                    structured_output_mode=mode,
                    max_output_tokens=self.max_output_tokens,
                    temperature=self.temperature,
                    timeout_seconds=self.timeout_seconds,
                    request_id=uuid.uuid4().hex,
                    metadata={"schema_name": getattr(schema, "__name__", "output")},
                )
            )
            raw = AIMessage(
                content=response.final_text,
                response_metadata={
                    "finish_reason": response.finish_reason,
                    "provider_request_id": response.provider_request_id,
                },
            )
            try:
                value = extract_json_value(response.final_text)
                parsed = (
                    schema.model_validate(value)
                    if isinstance(schema, type) and issubclass(schema, BaseModel)
                    else value
                )
            except Exception as error:
                if include_raw:
                    return {"raw": raw, "parsed": None, "parsing_error": error}
                raise
            if include_raw:
                return {"raw": raw, "parsed": parsed, "parsing_error": None}
            return parsed

        return RunnableLambda(invoke_structured)


def _to_model_message(message: BaseMessage) -> ModelMessage:
    if isinstance(message, SystemMessage):
        role = "system"
    elif isinstance(message, AIMessage):
        role = "assistant"
    else:
        role = "user"
    content = (
        message.content
        if isinstance(message.content, str)
        else json.dumps(message.content, ensure_ascii=False, default=str)
    )
    return ModelMessage(role=role, content=content)


def _schema_json(schema: dict | type) -> dict:
    if isinstance(schema, dict):
        if "function" in schema and isinstance(schema["function"], dict):
            parameters = schema["function"].get("parameters")
            if isinstance(parameters, dict):
                return parameters
        return schema
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_json_schema()
    return TypeAdapter(schema).json_schema()
