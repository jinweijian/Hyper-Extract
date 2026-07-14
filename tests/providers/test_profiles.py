from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from hyperextract.providers.contracts import ProfileConfigurationError
from hyperextract.providers.profiles import (
    ModelProfile,
    ProfileCapabilities,
    load_profile,
    load_profiles_from_toml,
)


def _minimal_profile_data() -> dict:
    return {
        "llm": "openai:gpt-4o@https://api.openai.com/v1",
        "llm_api_key_env": "OPENAI_API_KEY",
    }


def test_load_profile_fills_conservative_defaults():
    profile = load_profile("my-profile", _minimal_profile_data())
    assert profile.name == "my-profile"
    assert profile.llm == "openai:gpt-4o@https://api.openai.com/v1"
    assert profile.llm_api_key_env == "OPENAI_API_KEY"
    assert profile.transport == "openai_chat"
    assert profile.embedder == ""
    assert profile.embedder_api_key_env == ""
    assert profile.llm_rate_limit_group is None
    assert profile.embedder_rate_limit_group is None
    assert profile.capabilities.transport == "openai_chat"
    assert profile.capabilities.structured_output_modes == ["text_json"]
    assert profile.capabilities.preferred_structured_output_mode == "text_json"
    assert profile.capabilities.structured_output_fallback_order == []
    assert profile.capabilities.reasoning_content_mode == "none"
    assert profile.capabilities.output_token_parameter == "max_tokens"
    assert profile.capabilities.supported_parameters == {
        "max_output_tokens",
        "timeout_seconds",
    }
    assert profile.capabilities.recommended_concurrency == 1
    assert profile.embedding_capabilities is None
    assert profile.recovery.validation_repair_attempts == 1
    assert profile.recovery.validation_retry_attempts == 3
    assert profile.recovery.transient_retry_attempts == 4
    assert profile.recovery.invalid_list_item_policy == "quarantine"
    assert profile.recovery.invalid_item_ratio_threshold == 0.2
    assert profile.probe_required is False
    assert profile.request_timeout == 900
    assert profile.max_tokens is None


def test_preferred_structured_output_mode_must_be_in_modes():
    with pytest.raises(ValidationError, match="preferred_structured_output_mode"):
        ModelProfile(
            name="test",
            llm="openai:gpt-4o@https://api.openai.com/v1",
            llm_api_key_env="OPENAI_API_KEY",
            capabilities=ProfileCapabilities(
                structured_output_modes=["text_json"],
                preferred_structured_output_mode="native",
            ),
        )


def test_fallback_order_mode_not_in_modes_rejected():
    with pytest.raises(ValidationError, match="structured_output_fallback_order"):
        ModelProfile(
            name="test",
            llm="openai:gpt-4o@https://api.openai.com/v1",
            llm_api_key_env="OPENAI_API_KEY",
            capabilities=ProfileCapabilities(
                structured_output_modes=["text_json"],
                preferred_structured_output_mode="text_json",
                structured_output_fallback_order=["native"],
            ),
        )


def test_fallback_order_contains_auto_rejected():
    with pytest.raises(ValidationError, match="auto"):
        ModelProfile(
            name="test",
            llm="openai:gpt-4o@https://api.openai.com/v1",
            llm_api_key_env="OPENAI_API_KEY",
            capabilities=ProfileCapabilities(
                structured_output_modes=["auto", "text_json"],
                preferred_structured_output_mode="text_json",
                structured_output_fallback_order=["auto"],
            ),
        )


def test_public_fingerprint_is_stable_across_calls():
    profile = ModelProfile(
        name="test",
        llm="openai:gpt-4o@https://api.openai.com/v1",
        llm_api_key_env="OPENAI_API_KEY",
    )
    assert profile.public_fingerprint() == profile.public_fingerprint()


def test_public_fingerprint_excludes_secret_values(monkeypatch):
    profile = ModelProfile(
        name="test",
        llm="openai:gpt-4o@https://api.openai.com/v1",
        llm_api_key_env="OPENAI_API_KEY",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-first-secret-value")
    fp1 = profile.public_fingerprint()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-second-secret-value")
    fp2 = profile.public_fingerprint()
    assert fp1 == fp2


def test_public_descriptor_includes_fingerprint():
    profile = ModelProfile(
        name="test",
        llm="openai:gpt-4o@https://api.openai.com/v1",
        llm_api_key_env="OPENAI_API_KEY",
    )
    descriptor = profile.public_descriptor()
    assert descriptor["name"] == "test"
    assert "fingerprint" in descriptor
    assert descriptor["fingerprint"] == profile.public_fingerprint()
    assert "llm_api_key_env" not in descriptor
    assert "embedder_api_key_env" not in descriptor


def test_load_profiles_from_toml_reads_sections(tmp_path):
    toml_path = tmp_path / "profiles.toml"
    toml_path.write_text(
        """
[profiles.alpha]
llm = "openai:gpt-4o@https://api.openai.com/v1"
llm_api_key_env = "OPENAI_API_KEY"

[profiles.beta]
llm = "anthropic:claude-3@https://api.anthropic.com"
llm_api_key_env = "ANTHROPIC_API_KEY"
transport = "anthropic_messages"
""",
        encoding="utf-8",
    )
    profiles = load_profiles_from_toml(toml_path)
    assert set(profiles.keys()) == {"alpha", "beta"}
    assert profiles["alpha"].transport == "openai_chat"
    assert profiles["beta"].transport == "anthropic_messages"


# --- openai-compatible-default seed (env-resolved at descriptor time) ---


def _clear_default_env(monkeypatch):
    for var in (
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_default_seed_resolves_from_env(monkeypatch):
    _clear_default_env(monkeypatch)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    from hyperextract.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    descriptor = registry.public_descriptor("openai-compatible-default")
    assert "gpt-4o" in descriptor["llm"]
    assert "https://api.openai.com/v1" in descriptor["llm"]
    caps = descriptor["capabilities"]
    assert caps["structured_output_modes"] == ["text_json"]
    assert caps["preferred_structured_output_mode"] == "text_json"
    assert caps["structured_output_fallback_order"] == ["text_json"]


def test_default_seed_missing_model_raises(monkeypatch):
    _clear_default_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    from hyperextract.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    with pytest.raises(ProfileConfigurationError) as exc_info:
        registry.get("openai-compatible-default")
    assert exc_info.value.code == "OPENAI_MODEL_MISSING"


def test_default_seed_missing_model_raises_from_descriptor(monkeypatch):
    _clear_default_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    from hyperextract.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    with pytest.raises(ProfileConfigurationError) as exc_info:
        registry.public_descriptor("openai-compatible-default")
    assert exc_info.value.code == "OPENAI_MODEL_MISSING"


def test_default_seed_embedding_independent_of_llm(monkeypatch):
    _clear_default_env(monkeypatch)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    from hyperextract.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    descriptor = registry.public_descriptor("openai-compatible-default")
    assert descriptor["embedder"] == ""
    assert descriptor["embedder"] != descriptor["llm"]
    assert descriptor["embedding_capabilities"] is None


def test_default_seed_embedding_resolves_when_configured(monkeypatch):
    _clear_default_env(monkeypatch)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-emb")

    from hyperextract.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    profile = registry.get("openai-compatible-default")
    assert "text-embedding-3-small" in profile.embedder
    assert profile.embedder_api_key_env == "EMBEDDING_API_KEY"
    assert profile.embedding_capabilities is not None
