from __future__ import annotations

import os
from pathlib import Path

from hyperextract.providers.contracts import ProfileConfigurationError
from hyperextract.providers.profiles import (
    ModelProfile,
    ProfileCapabilities,
    ProfileEmbeddingCapabilities,
    load_profiles_from_toml,
)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_PROFILE_NAME = "openai-compatible-default"


class ProviderRegistry:
    def __init__(self, toml_path: Path | None = None) -> None:
        self._toml_path = toml_path
        self._profiles: dict[str, ModelProfile] = {}
        if toml_path is not None:
            self._profiles = load_profiles_from_toml(toml_path)

    def register(self, profile: ModelProfile) -> None:
        self._profiles[profile.name] = profile

    def get(self, name: str) -> ModelProfile:
        if name in self._profiles:
            return self._profiles[name]
        if name == _DEFAULT_PROFILE_NAME:
            return self._build_default_profile()
        raise KeyError(name)

    def list(self) -> list[str]:
        names = set(self._profiles.keys())
        names.add(_DEFAULT_PROFILE_NAME)
        return sorted(names)

    def public_descriptor(self, name: str) -> dict:
        return self.get(name).public_descriptor()

    def _build_default_profile(self) -> ModelProfile:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        model = os.environ.get("OPENAI_MODEL", "").strip()
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or _DEFAULT_BASE_URL

        if api_key and not model:
            raise ProfileConfigurationError(
                "OPENAI_MODEL is required when OPENAI_API_KEY is set but "
                "OPENAI_MODEL is missing or empty",
                code="OPENAI_MODEL_MISSING",
            )

        llm = f"openai:{model}@{base_url}" if model else ""

        embedding_model = os.environ.get("EMBEDDING_MODEL", "").strip()
        if embedding_model:
            embedding_base_url = (
                os.environ.get("EMBEDDING_BASE_URL", "").strip() or _DEFAULT_BASE_URL
            )
            embedder = f"openai:{embedding_model}@{embedding_base_url}"
            embedder_api_key_env = "EMBEDDING_API_KEY"
            embedding_capabilities = ProfileEmbeddingCapabilities()
        else:
            embedder = ""
            embedder_api_key_env = ""
            embedding_capabilities = None

        return ModelProfile(
            name=_DEFAULT_PROFILE_NAME,
            transport="openai_chat",
            llm=llm,
            llm_api_key_env="OPENAI_API_KEY",
            embedder=embedder,
            embedder_api_key_env=embedder_api_key_env,
            capabilities=ProfileCapabilities(
                transport="openai_chat",
                structured_output_modes=["text_json"],
                preferred_structured_output_mode="text_json",
                structured_output_fallback_order=["text_json"],
            ),
            embedding_capabilities=embedding_capabilities,
        )
