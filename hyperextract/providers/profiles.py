from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from hyperextract.documents.checkpoint import fingerprint
from hyperextract.providers.contracts import OutputMode


class ProfileCapabilities(BaseModel):
    transport: Literal["openai_chat", "anthropic_messages"] = "openai_chat"
    structured_output_modes: list[OutputMode] = ["text_json"]
    preferred_structured_output_mode: OutputMode = "text_json"
    structured_output_fallback_order: list[OutputMode] = []
    reasoning_content_mode: Literal[
        "none", "inline_tags", "separate_field", "content_blocks"
    ] = "none"
    output_token_parameter: str = "max_tokens"
    supported_parameters: set[str] = {"max_output_tokens", "timeout_seconds"}
    context_tokens: int | None = None
    max_output_tokens: int | None = None
    recommended_concurrency: int = 1

    @model_validator(mode="after")
    def _validate_output_modes(self) -> ProfileCapabilities:
        if self.preferred_structured_output_mode not in self.structured_output_modes:
            raise ValueError(
                f"preferred_structured_output_mode "
                f"{self.preferred_structured_output_mode!r} is not in "
                f"structured_output_modes {self.structured_output_modes}"
            )
        for mode in self.structured_output_fallback_order:
            if mode == "auto":
                raise ValueError(
                    "structured_output_fallback_order must not contain 'auto'; "
                    f"found: {self.structured_output_fallback_order}"
                )
            if mode not in self.structured_output_modes:
                raise ValueError(
                    f"structured_output_fallback_order mode {mode!r} is not in "
                    f"structured_output_modes {self.structured_output_modes}"
                )
        return self


class ProfileEmbeddingCapabilities(BaseModel):
    transport: Literal["openai_embeddings"] = "openai_embeddings"
    accepts_token_ids: bool = False
    max_batch_items: int | None = None
    max_batch_tokens: int | None = None
    max_input_tokens_per_item: int | None = None
    supports_dimensions: bool = False
    empty_input_policy: Literal["reject", "quarantine", "zero_vector"] = "reject"


class ProfileRecovery(BaseModel):
    validation_repair_attempts: int = 1
    validation_retry_attempts: int = 3
    transient_retry_attempts: int = 4
    invalid_list_item_policy: Literal["quarantine", "fail"] = "quarantine"
    invalid_item_ratio_threshold: float = 0.2


class ModelProfile(BaseModel):
    name: str
    transport: Literal["openai_chat", "anthropic_messages"] = "openai_chat"
    llm: str
    llm_api_key_env: str
    embedder: str = ""
    embedder_api_key_env: str = ""
    llm_rate_limit_group: str | None = None
    embedder_rate_limit_group: str | None = None
    capabilities: ProfileCapabilities = Field(default_factory=ProfileCapabilities)
    embedding_capabilities: ProfileEmbeddingCapabilities | None = None
    recovery: ProfileRecovery = Field(default_factory=ProfileRecovery)
    probe_required: bool = False
    request_timeout: int = 900
    max_tokens: int | None = None

    def _public_dict(self) -> dict[str, Any]:
        capabilities = self.capabilities.model_dump(mode="json")
        supported = capabilities.get("supported_parameters")
        if isinstance(supported, list):
            capabilities["supported_parameters"] = sorted(supported)
        embedding_capabilities = (
            self.embedding_capabilities.model_dump(mode="json")
            if self.embedding_capabilities is not None
            else None
        )
        recovery = self.recovery.model_dump(mode="json")
        return {
            "name": self.name,
            "transport": self.transport,
            "llm": self.llm,
            "embedder": self.embedder,
            "llm_rate_limit_group": self.llm_rate_limit_group,
            "embedder_rate_limit_group": self.embedder_rate_limit_group,
            "capabilities": capabilities,
            "embedding_capabilities": embedding_capabilities,
            "recovery": recovery,
            "probe_required": self.probe_required,
            "request_timeout": self.request_timeout,
            "max_tokens": self.max_tokens,
        }

    def public_fingerprint(self) -> str:
        return fingerprint(self._public_dict())

    def public_descriptor(self) -> dict[str, Any]:
        descriptor = self._public_dict()
        descriptor["fingerprint"] = fingerprint(descriptor)
        return descriptor


def load_profile(name: str, data: dict[str, Any]) -> ModelProfile:
    payload = {**data, "name": name}
    return ModelProfile.model_validate(payload)


def load_profiles_from_toml(path: str | Path) -> dict[str, ModelProfile]:
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    profiles: dict[str, ModelProfile] = {}
    for name, data in raw.get("profiles", {}).items():
        profiles[name] = load_profile(name, data)
    return profiles
