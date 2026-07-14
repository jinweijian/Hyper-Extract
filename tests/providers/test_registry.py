from __future__ import annotations

import pytest

from hyperextract.providers.profiles import ModelProfile, load_profile
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
