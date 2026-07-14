from __future__ import annotations

import tomllib
import warnings
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from hyperextract.documents.checkpoint import fingerprint
from hyperextract.providers.contracts import (
    EmbeddingCapabilities,
    ModelCapabilities,
    OutputMode,
)


class ProfileCapabilities(BaseModel):
    transport: Literal["openai_chat", "anthropic_messages"] = "openai_chat"
    structured_output_modes: list[OutputMode] = Field(
        default_factory=lambda: ["text_json"]
    )
    preferred_structured_output_mode: OutputMode = "text_json"
    structured_output_fallback_order: list[OutputMode] = Field(default_factory=list)
    reasoning_content_mode: Literal[
        "none", "inline_tags", "separate_field", "content_blocks"
    ] = "none"
    output_token_parameter: str = "max_tokens"
    supported_parameters: set[str] = Field(
        default_factory=lambda: {"max_output_tokens", "timeout_seconds"}
    )
    omit_if_unsupported: set[str] = Field(default_factory=set)
    context_tokens: int | None = None
    max_output_tokens: int | None = None
    recommended_concurrency: int = 1
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None

    @model_validator(mode="after")
    def _validate_output_modes(self) -> ProfileCapabilities:
        if self.preferred_structured_output_mode not in self.structured_output_modes:
            raise ValueError(
                "preferred_structured_output_mode "
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
        if len(set(self.structured_output_fallback_order)) != len(
            self.structured_output_fallback_order
        ):
            raise ValueError("structured_output_fallback_order contains duplicates")
        if self.recommended_concurrency < 1:
            raise ValueError("recommended_concurrency must be at least 1")
        for name in (
            "context_tokens",
            "max_output_tokens",
            "requests_per_minute",
            "tokens_per_minute",
        ):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be positive")
        if (
            self.context_tokens is not None
            and self.max_output_tokens is not None
            and self.max_output_tokens > self.context_tokens
        ):
            raise ValueError("max_output_tokens cannot exceed context_tokens")
        overlap = self.omit_if_unsupported & self.supported_parameters
        if overlap:
            raise ValueError(
                "omit_if_unsupported parameters are already supported: "
                f"{sorted(overlap)}"
            )
        return self

    def to_contract(self) -> ModelCapabilities:
        return ModelCapabilities.model_validate(self.model_dump())


class ProfileEmbeddingCapabilities(BaseModel):
    transport: Literal["openai_embeddings"] = "openai_embeddings"
    accepts_token_ids: bool = False
    max_batch_items: int | None = None
    max_batch_tokens: int | None = None
    max_input_tokens_per_item: int | None = None
    supports_dimensions: bool = False
    empty_input_policy: Literal["reject", "quarantine", "zero_vector"] = "reject"

    @model_validator(mode="after")
    def _validate_limits(self) -> ProfileEmbeddingCapabilities:
        for name in (
            "max_batch_items",
            "max_batch_tokens",
            "max_input_tokens_per_item",
        ):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be positive")
        return self

    def to_contract(self) -> EmbeddingCapabilities:
        return EmbeddingCapabilities.model_validate(self.model_dump())


class ProfileRecovery(BaseModel):
    validation_repair_attempts: int = 1
    validation_retry_attempts: int = 3
    transient_retry_attempts: int = 4
    invalid_list_item_policy: Literal["quarantine", "fail"] = "quarantine"
    invalid_item_ratio_threshold: float = 0.2
    fallback_attempts: int = 2
    rate_limit_attempts: int = 8
    max_rate_limit_elapsed_seconds: float = 1800
    base_delay_seconds: float = 2
    max_delay_seconds: float = 120

    @model_validator(mode="after")
    def _validate_budgets(self) -> ProfileRecovery:
        attempts = (
            self.validation_repair_attempts,
            self.validation_retry_attempts,
            self.transient_retry_attempts,
            self.fallback_attempts,
            self.rate_limit_attempts,
        )
        if any(value < 0 for value in attempts):
            raise ValueError("recovery attempt budgets cannot be negative")
        if not 0 <= self.invalid_item_ratio_threshold <= 1:
            raise ValueError("invalid_item_ratio_threshold must be between 0 and 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("recovery delays cannot be negative")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds cannot be below base_delay_seconds")
        return self


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
    probe_ttl_hours: int = 24
    request_timeout: int = 900
    max_tokens: int | None = None

    @property
    def structured_output_mode(self) -> OutputMode:
        return self.capabilities.preferred_structured_output_mode

    @property
    def output_repair_attempts(self) -> int:
        return self.recovery.validation_repair_attempts

    @model_validator(mode="after")
    def _validate_profile(self) -> ModelProfile:
        if self.transport != self.capabilities.transport:
            raise ValueError("profile transport and capability transport must match")
        if bool(self.embedder) != bool(self.embedding_capabilities):
            raise ValueError(
                "embedder and embedding_capabilities must be configured together"
            )
        if bool(self.embedder) != bool(self.embedder_api_key_env):
            raise ValueError(
                "embedder and embedder_api_key_env must be configured together"
            )
        if self.request_timeout < 1 or self.probe_ttl_hours < 1:
            raise ValueError("request_timeout and probe_ttl_hours must be positive")
        return self

    def _public_dict(self) -> dict[str, Any]:
        capabilities = self.capabilities.model_dump(mode="json")
        for name in ("supported_parameters", "omit_if_unsupported"):
            value = capabilities.get(name)
            if isinstance(value, list):
                capabilities[name] = sorted(value)
        embedding_capabilities = (
            self.embedding_capabilities.model_dump(mode="json")
            if self.embedding_capabilities is not None
            else None
        )
        return {
            "name": self.name,
            "transport": self.transport,
            "llm": self.llm,
            "embedder": self.embedder,
            "llm_rate_limit_group": self.llm_rate_limit_group,
            "embedder_rate_limit_group": self.embedder_rate_limit_group,
            "capabilities": capabilities,
            "embedding_capabilities": embedding_capabilities,
            "recovery": self.recovery.model_dump(mode="json"),
            "probe_required": self.probe_required,
            "probe_ttl_hours": self.probe_ttl_hours,
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
    if "capabilities" not in data:
        warnings.warn(
            f"Profile {name!r} uses the legacy format without capabilities; "
            "conservative text_json defaults were applied",
            DeprecationWarning,
            stacklevel=2,
        )
    payload = {**_upgrade_legacy_profile(data), "name": name}
    return ModelProfile.model_validate(payload)


def _upgrade_legacy_profile(data: dict[str, Any]) -> dict[str, Any]:
    upgraded = dict(data)
    mode = upgraded.pop("structured_output_mode", None)
    repairs = upgraded.pop("output_repair_attempts", None)
    if mode is not None and "capabilities" not in upgraded:
        upgraded["capabilities"] = {
            "structured_output_modes": [mode],
            "preferred_structured_output_mode": mode,
            "structured_output_fallback_order": [mode],
        }
    elif "capabilities" not in upgraded and upgraded.get("transport"):
        upgraded["capabilities"] = {"transport": upgraded["transport"]}
    if repairs is not None:
        recovery = dict(upgraded.get("recovery", {}))
        recovery.setdefault("validation_repair_attempts", repairs)
        upgraded["recovery"] = recovery
    if upgraded.get("embedder") and "embedding_capabilities" not in upgraded:
        upgraded["embedding_capabilities"] = {}
    return upgraded


def load_profiles_from_toml(path: str | Path) -> dict[str, ModelProfile]:
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    return {
        name: load_profile(name, data) for name, data in raw.get("profiles", {}).items()
    }
