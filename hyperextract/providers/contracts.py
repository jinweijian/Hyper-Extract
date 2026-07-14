from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

OutputMode = Literal["auto", "native", "tool", "json_object", "text_json"]


class ProfileConfigurationError(ValueError):
    """Configuration error for a model profile, carrying a stable machine code."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ModelMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class GenerationRequest(BaseModel):
    operation: str
    messages: list[ModelMessage]
    output_schema: dict | None = None
    structured_output: bool = False
    max_output_tokens: int | None = None
    temperature: float | None = None
    timeout_seconds: int | None = None
    request_id: str
    metadata: dict[str, str] = Field(default_factory=dict)


class GenerationResponse(BaseModel):
    request_id: str
    final_text: str
    reasoning_text: str | None = None
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    provider_request_id: str | None = None
    raw_response_ref: str | None = None


class EmbeddingRequest(BaseModel):
    inputs: list[str]
    dimensions: int | None = None
    request_id: str
    metadata: dict[str, str] = Field(default_factory=dict)


class EmbeddingItemResult(BaseModel):
    input_index: int
    vector: list[float] | None = None
    status: Literal["completed", "quarantined"]
    error_reason: str | None = None


class EmbeddingResponse(BaseModel):
    request_id: str
    items: list[EmbeddingItemResult]
    input_tokens: int | None = None
    provider_request_id: str | None = None


class ModelCapabilities(BaseModel):
    transport: Literal["openai_chat", "anthropic_messages"]
    structured_output_modes: list[OutputMode]
    preferred_structured_output_mode: OutputMode
    reasoning_content_mode: Literal[
        "none", "inline_tags", "separate_field", "content_blocks"
    ]
    output_token_parameter: str
    supported_parameters: set[str]
    context_tokens: int | None = None
    max_output_tokens: int | None = None
    recommended_concurrency: int = 1


class EmbeddingCapabilities(BaseModel):
    transport: Literal["openai_embeddings"]
    accepts_token_ids: bool = False
    max_batch_items: int | None = None
    max_batch_tokens: int | None = None
    max_input_tokens_per_item: int | None = None
    supports_dimensions: bool = False
    empty_input_policy: Literal["reject", "quarantine", "zero_vector"] = "reject"


class CanonicalModelFailure(BaseModel):
    request_id: str
    category: str
    reason: str
    http_status: int | None = None
    provider_code: str | None = None
    retry_after_seconds: float | None = None
    raw_message: str | None = None


_LEGAL_RECOVERY_TARGETS: dict[str, frozenset[str]] = {
    "retry": frozenset({"request"}),
    "fallback": frozenset({"request"}),
    "repair": frozenset({"item", "batch"}),
    "split": frozenset({"batch", "chunk"}),
    "replan": frozenset({"chunk"}),
    "quarantine": frozenset({"item", "batch", "chunk"}),
    "fail": frozenset({"request", "batch", "chunk", "run"}),
    "circuit_break": frozenset({"rate_limit_group"}),
}


class RecoveryDecision(BaseModel):
    action: Literal[
        "retry",
        "fallback",
        "repair",
        "split",
        "replan",
        "quarantine",
        "fail",
        "circuit_break",
    ]
    target: Literal["item", "batch", "chunk", "request", "run", "rate_limit_group"]
    reason: str
    delay_seconds: float = 0
    consume_attempt: bool = True

    @model_validator(mode="after")
    def _validate_action_target(self) -> RecoveryDecision:
        legal = _LEGAL_RECOVERY_TARGETS.get(self.action, frozenset())
        if self.target not in legal:
            raise ValueError(
                f"Invalid recovery target {self.target!r} for action "
                f"{self.action!r}; legal targets: {sorted(legal)}"
            )
        return self
