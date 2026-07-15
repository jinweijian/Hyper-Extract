from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from pathlib import Path

from hyperextract.providers.adapters import (
    AnthropicAdapter,
    OpenAIChatAdapter,
    OpenAIEmbeddingAdapter,
)
from hyperextract.providers.contracts import ProfileConfigurationError
from hyperextract.providers.profiles import (
    ModelProfile,
    ProfileCapabilities,
    ProfileEmbeddingCapabilities,
    load_profiles_from_toml,
)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_PROFILE_NAME = "openai-compatible-default"
LEGACY_MINIMAX_PROFILE = "minimax-course-default"


@dataclass(frozen=True)
class ModelReference:
    provider: str
    model: str
    base_url: str | None


class ProviderRegistry:
    """One profile and adapter registry shared by API, CLI, and Python clients."""

    def __init__(self, toml_path: Path | None = None) -> None:
        self._toml_path = toml_path
        self._profiles = (
            load_profiles_from_toml(toml_path) if toml_path is not None else {}
        )

    def register(self, profile: ModelProfile) -> None:
        self._profiles[profile.name] = profile

    def get(self, name: str) -> ModelProfile:
        if name == LEGACY_MINIMAX_PROFILE:
            warnings.warn(
                f"{LEGACY_MINIMAX_PROFILE!r} is deprecated; use an explicit "
                "minimax-m27 TOML profile with correctly spelled MINIMAX_* variables",
                DeprecationWarning,
                stacklevel=2,
            )
        if name in self._profiles:
            profile = self._profiles[name]
            if name != LEGACY_MINIMAX_PROFILE and profile.llm_api_key_env.startswith(
                "MIMIMAX_"
            ):
                warnings.warn(
                    "MIMIMAX_* is deprecated; rename the variables to MINIMAX_*",
                    DeprecationWarning,
                    stacklevel=2,
                )
            return profile
        if name == DEFAULT_PROFILE_NAME:
            return self._build_default_profile()
        if name == LEGACY_MINIMAX_PROFILE:
            return self._build_legacy_minimax_profile()
        raise KeyError(name)

    def list(self) -> list[str]:
        return sorted({*self._profiles, DEFAULT_PROFILE_NAME})

    def readiness_profiles(self) -> list[str]:
        """Profiles that make the deployment usable without requiring secrets."""
        return sorted(self._profiles) or [DEFAULT_PROFILE_NAME]

    def public_descriptor(self, name: str) -> dict:
        return self.get(name).public_descriptor()

    def validate(
        self,
        name: str,
        *,
        require_secrets: bool = True,
        require_embedder: bool = False,
        check_probe: bool = False,
    ) -> list[str]:
        profile = self.get(name)
        warnings_found: list[str] = []
        if not profile.llm:
            raise ProfileConfigurationError(
                f"Profile {name!r} does not select an LLM model",
                code="LLM_MODEL_MISSING",
            )
        parse_model_reference(profile.llm)
        if not profile.llm_api_key_env.strip():
            raise ProfileConfigurationError(
                f"Profile {name!r} has an empty LLM credential environment name",
                code="MODEL_PROFILE_ENV_NAME_MISSING",
            )
        if require_embedder and (
            not profile.embedder or profile.embedding_capabilities is None
        ):
            raise ProfileConfigurationError(
                f"Profile {name!r} does not configure an embedder",
                code="EMBEDDER_MISSING",
            )
        if profile.embedder:
            parse_model_reference(profile.embedder)
            if not profile.embedder_api_key_env.strip():
                raise ProfileConfigurationError(
                    f"Profile {name!r} has an empty embedding credential "
                    "environment name",
                    code="MODEL_PROFILE_ENV_NAME_MISSING",
                )
        _validate_parameter_mapping(profile)
        if require_secrets:
            self._required_env(profile.llm_api_key_env)
            if profile.embedder:
                self._required_env(profile.embedder_api_key_env)
        if not profile.probe_required:
            warnings_found.append("probe_not_required")
        if profile.embedding_capabilities is not None:
            if profile.embedding_capabilities.empty_input_policy == "zero_vector":
                warnings_found.append("embedding_zero_vector_enabled")
        if check_probe:
            from hyperextract.providers.probe import ensure_probe_eligibility

            ensure_probe_eligibility(profile)
        return warnings_found

    def create_generation_adapter(self, name: str, *, client=None, api_key=None):
        profile = self.get(name)
        reference = parse_model_reference(profile.llm)
        api_key = api_key or self._required_env(profile.llm_api_key_env)
        common = {
            "model": reference.model,
            "base_url": reference.base_url,
            "api_key": api_key,
            "capabilities": profile.capabilities.to_contract(),
            "client": client,
            "max_retries": 0,
        }
        if profile.transport == "anthropic_messages":
            return AnthropicAdapter(**common)
        return OpenAIChatAdapter(**common)

    def create_embedding_adapter(
        self,
        name: str,
        *,
        client=None,
        encoding=None,
        scheduler=None,
        api_key=None,
    ):
        profile = self.get(name)
        if not profile.embedder or profile.embedding_capabilities is None:
            raise ProfileConfigurationError(
                f"Profile {name!r} does not configure an embedder",
                code="EMBEDDER_MISSING",
            )
        reference = parse_model_reference(profile.embedder)
        return OpenAIEmbeddingAdapter(
            model=reference.model,
            base_url=reference.base_url,
            api_key=api_key or self._required_env(profile.embedder_api_key_env),
            capabilities=profile.embedding_capabilities.to_contract(),
            client=client,
            encoding=encoding,
            max_retries=0,
            item_failure_policy=profile.embedding_capabilities.item_failure_policy,
            scheduler=scheduler,
        )

    @staticmethod
    def _required_env(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise ProfileConfigurationError(
                "MODEL_PROFILE_ENV_MISSING: Required model credential environment "
                f"variable is missing: {name}",
                code="MODEL_PROFILE_ENV_MISSING",
            )
        return value

    def _build_default_profile(self) -> ModelProfile:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        model = os.environ.get("OPENAI_MODEL", "").strip()
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or DEFAULT_BASE_URL
        if api_key and not model:
            raise ProfileConfigurationError(
                "OPENAI_MODEL is required when OPENAI_API_KEY is configured",
                code="OPENAI_MODEL_MISSING",
            )
        llm = f"openai:{model}@{base_url}" if model else ""

        embedding_model = os.environ.get("EMBEDDING_MODEL", "").strip()
        if embedding_model:
            embedding_base_url = (
                os.environ.get("EMBEDDING_BASE_URL", "").strip() or DEFAULT_BASE_URL
            )
            embedder = f"openai:{embedding_model}@{embedding_base_url}"
            embedding_capabilities = ProfileEmbeddingCapabilities()
            embedder_api_key_env = "EMBEDDING_API_KEY"
        else:
            embedder = ""
            embedding_capabilities = None
            embedder_api_key_env = ""

        return ModelProfile(
            name=DEFAULT_PROFILE_NAME,
            llm=llm,
            llm_api_key_env="OPENAI_API_KEY",
            embedder=embedder,
            embedder_api_key_env=embedder_api_key_env,
            llm_rate_limit_group="openai-compatible-default",
            embedder_rate_limit_group=(
                "openai-compatible-embeddings" if embedder else None
            ),
            capabilities=ProfileCapabilities(
                structured_output_modes=["text_json"],
                preferred_structured_output_mode="text_json",
                structured_output_fallback_order=["text_json"],
            ),
            embedding_capabilities=embedding_capabilities,
        )

    def _build_legacy_minimax_profile(self) -> ModelProfile:
        new_values = _env_group("MINIMAX")
        old_values = _env_group("MIMIMAX")
        if _partially_configured(new_values) or _partially_configured(old_values):
            raise ProfileConfigurationError(
                "MiniMax variables must be configured as one complete MINIMAX_* "
                "or MIMIMAX_* group; groups cannot be mixed",
                code="MINIMAX_ENV_GROUP_INCOMPLETE",
            )
        if all(new_values.values()):
            prefix = "MINIMAX"
            values = new_values
        elif all(old_values.values()):
            prefix = "MIMIMAX"
            values = old_values
        else:
            raise ProfileConfigurationError(
                "Legacy MiniMax profile requires a complete MINIMAX_* variable group",
                code="MINIMAX_ENV_GROUP_MISSING",
            )
        embedding_model = os.environ.get("EMBEDDING_MODEL", "").strip()
        embedding_url = os.environ.get("EMBEDDING_BASE_URL", "").strip()
        return ModelProfile(
            name=LEGACY_MINIMAX_PROFILE,
            llm=f"openai:{values['model']}@{values['base_url']}",
            llm_api_key_env=f"{prefix}_API_KEY",
            embedder=(
                f"openai:{embedding_model}@{embedding_url}"
                if embedding_model and embedding_url
                else ""
            ),
            embedder_api_key_env="EMBEDDING_API_KEY" if embedding_model else "",
            capabilities=ProfileCapabilities(
                structured_output_modes=["text_json"],
                preferred_structured_output_mode="text_json",
                structured_output_fallback_order=["text_json"],
                reasoning_content_mode="separate_field",
            ),
            embedding_capabilities=(
                ProfileEmbeddingCapabilities(empty_input_policy="quarantine")
                if embedding_model
                else None
            ),
        )


def parse_model_reference(value: str) -> ModelReference:
    value = value.strip()
    if not value:
        raise ProfileConfigurationError(
            "Model reference is empty", code="MODEL_REFERENCE_EMPTY"
        )
    provider, separator, remainder = value.partition(":")
    if not separator:
        raise ProfileConfigurationError(
            f"Model reference must use provider:model@base_url syntax: {value!r}",
            code="MODEL_REFERENCE_INVALID",
        )
    provider = provider.strip()
    model, at, base_url = remainder.partition("@")
    model = model.strip()
    base_url = base_url.strip()
    if not provider or not model:
        raise ProfileConfigurationError(
            f"Invalid model reference: {value!r}",
            code="MODEL_REFERENCE_INVALID",
        )
    if at and not base_url:
        raise ProfileConfigurationError(
            f"Model reference has an empty base URL: {value!r}",
            code="MODEL_REFERENCE_INVALID",
        )
    return ModelReference(
        provider=provider, model=model, base_url=base_url if at else None
    )


def _validate_parameter_mapping(profile: ModelProfile) -> None:
    known = {"max_output_tokens", "temperature", "timeout_seconds"}
    capabilities = profile.capabilities
    declared = capabilities.supported_parameters | capabilities.omit_if_unsupported
    unknown = declared - known
    if unknown:
        raise ProfileConfigurationError(
            f"Profile {profile.name!r} declares unknown parameters: {sorted(unknown)}",
            code="PARAMETER_MAPPING_INVALID",
        )
    required = {"timeout_seconds"}
    if profile.max_tokens is not None or capabilities.max_output_tokens is not None:
        required.add("max_output_tokens")
    missing = required - declared
    if missing:
        raise ProfileConfigurationError(
            f"Profile {profile.name!r} has no mapping policy for parameters: "
            f"{sorted(missing)}",
            code="PARAMETER_MAPPING_INCOMPLETE",
        )
    if (
        "max_output_tokens" in capabilities.supported_parameters
        and not capabilities.output_token_parameter.strip()
    ):
        raise ProfileConfigurationError(
            f"Profile {profile.name!r} has an empty output token parameter mapping",
            code="PARAMETER_MAPPING_INCOMPLETE",
        )


def _env_group(prefix: str) -> dict[str, str]:
    return {
        "model": os.environ.get(f"{prefix}_MODEL", "").strip(),
        "base_url": os.environ.get(f"{prefix}_BASE_URL", "").strip(),
        "api_key": os.environ.get(f"{prefix}_API_KEY", "").strip(),
    }


def _partially_configured(values: dict[str, str]) -> bool:
    configured = sum(bool(value) for value in values.values())
    return 0 < configured < len(values)
