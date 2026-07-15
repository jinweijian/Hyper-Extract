from __future__ import annotations

import pytest

from hyperextract.providers.profiles import (
    ModelProfile,
    ProfileEmbeddingCapabilities,
    ProfileRecovery,
    load_profile,
)
from hyperextract.providers.contracts import ProfileConfigurationError
from hyperextract.providers.registry import ProviderRegistry


def _make_profile(name: str = "alpha") -> ModelProfile:
    return load_profile(
        name,
        {
            "llm": "openai:gpt-4o@https://api.openai.com/v1",
            "llm_api_key_env": "OPENAI_API_KEY",
        },
    )


def test_register_get_list_round_trip():
    registry = ProviderRegistry()
    profile = _make_profile("alpha")
    registry.register(profile)
    assert registry.get("alpha") is profile
    assert "alpha" in registry.list()


def test_get_missing_raises_key_error():
    registry = ProviderRegistry()
    with pytest.raises(KeyError):
        registry.get("nonexistent")


def test_public_descriptor_delegates_to_profile():
    registry = ProviderRegistry()
    profile = _make_profile("alpha")
    registry.register(profile)
    descriptor = registry.public_descriptor("alpha")
    assert descriptor["name"] == "alpha"
    assert "fingerprint" in descriptor
    assert descriptor["fingerprint"] == profile.public_fingerprint()


def test_seeded_default_present_without_toml(monkeypatch):
    for var in (
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    registry = ProviderRegistry()
    assert "openai-compatible-default" in registry.list()
    profile = registry.get("openai-compatible-default")
    assert profile.name == "openai-compatible-default"


def test_toml_profile_extends_registry(monkeypatch, tmp_path):
    for var in (
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    toml_path = tmp_path / "profiles.toml"
    toml_path.write_text(
        """
[profiles.custom]
llm = "openai:gpt-4o-mini@https://api.openai.com/v1"
llm_api_key_env = "OPENAI_API_KEY"
""",
        encoding="utf-8",
    )
    registry = ProviderRegistry(toml_path=toml_path)
    names = set(registry.list())
    assert "custom" in names
    assert "openai-compatible-default" in names
    assert registry.get("custom").llm == "openai:gpt-4o-mini@https://api.openai.com/v1"


def test_toml_can_override_default_seed(monkeypatch, tmp_path):
    for var in (
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    toml_path = tmp_path / "profiles.toml"
    toml_path.write_text(
        """
[profiles.openai-compatible-default]
llm = "openai:gpt-4o-mini@https://custom.example.com/v1"
llm_api_key_env = "OPENAI_API_KEY"
""",
        encoding="utf-8",
    )
    registry = ProviderRegistry(toml_path=toml_path)
    profile = registry.get("openai-compatible-default")
    assert profile.llm == "openai:gpt-4o-mini@https://custom.example.com/v1"


def test_embedding_failure_policy_is_independent_from_structured_list_policy(
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-llm")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-embedding")
    profile = ModelProfile(
        name="independent-policies",
        llm="openai:gpt-4o@https://api.openai.com/v1",
        llm_api_key_env="OPENAI_API_KEY",
        embedder="openai:text-embedding-3-small@https://api.openai.com/v1",
        embedder_api_key_env="EMBEDDING_API_KEY",
        embedding_capabilities=ProfileEmbeddingCapabilities(
            item_failure_policy="quarantine"
        ),
        recovery=ProfileRecovery(invalid_list_item_policy="fail"),
    )
    registry = ProviderRegistry()
    registry.register(profile)

    adapter = registry.create_embedding_adapter(
        profile.name,
        client=object(),
        encoding=object(),
    )

    assert adapter._item_failure_policy == "quarantine"


def test_course_service_validation_requires_embedder():
    registry = ProviderRegistry()
    registry.register(_make_profile("llm-only"))

    with pytest.raises(ProfileConfigurationError) as error:
        registry.validate(
            "llm-only",
            require_secrets=False,
            require_embedder=True,
        )

    assert error.value.code == "EMBEDDER_MISSING"


def test_validation_rejects_malformed_model_reference_before_adapter_creation():
    registry = ProviderRegistry()
    registry.register(
        ModelProfile(
            name="malformed",
            llm="missing-provider-separator",
            llm_api_key_env="OPENAI_API_KEY",
        )
    )

    with pytest.raises(ProfileConfigurationError) as error:
        registry.validate("malformed", require_secrets=False)

    assert error.value.code == "MODEL_REFERENCE_INVALID"


def test_validation_rejects_empty_explicit_base_url():
    registry = ProviderRegistry()
    registry.register(
        ModelProfile(
            name="empty-base",
            llm="openai:model@",
            llm_api_key_env="OPENAI_API_KEY",
        )
    )

    with pytest.raises(ProfileConfigurationError) as error:
        registry.validate("empty-base", require_secrets=False)

    assert error.value.code == "MODEL_REFERENCE_INVALID"


def test_validation_can_enforce_required_probe(monkeypatch):
    registry = ProviderRegistry()
    profile = _make_profile("probe-required").model_copy(
        update={"probe_required": True}
    )
    registry.register(profile)
    calls = []
    monkeypatch.setattr(
        "hyperextract.providers.probe.ensure_probe_eligibility",
        lambda selected: calls.append(selected.name),
    )

    registry.validate(
        profile.name,
        require_secrets=False,
        check_probe=True,
    )

    assert calls == [profile.name]
