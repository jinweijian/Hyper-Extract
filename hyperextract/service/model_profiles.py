from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hyperextract.providers.profiles import ModelProfile
from hyperextract.providers.registry import ProviderRegistry


@dataclass(frozen=True)
class ModelProfileSpec:
    name: str
    llm: str
    llm_api_key_env: str
    embedder: str
    embedder_api_key_env: str
    structured_output_mode: str
    output_repair_attempts: int
    request_timeout: int
    max_tokens: int | None


@dataclass(frozen=True)
class ResolvedModelProfile:
    """Worker-only profile with dereferenced secrets."""

    profile: ModelProfile
    llm_api_key: str
    embedder_api_key: str

    @property
    def name(self) -> str:
        return self.profile.name

    @property
    def llm(self) -> str:
        return self.profile.llm

    @property
    def embedder(self) -> str:
        return self.profile.embedder

    @property
    def structured_output_mode(self) -> str:
        return self.profile.structured_output_mode

    @property
    def output_repair_attempts(self) -> int:
        return self.profile.output_repair_attempts

    @property
    def request_timeout(self) -> int:
        return self.profile.request_timeout

    @property
    def max_tokens(self) -> int | None:
        return self.profile.max_tokens or self.profile.capabilities.max_output_tokens


class ModelProfileRegistry:
    """Compatibility facade over the shared provider registry."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.providers = ProviderRegistry(path)

    def get_spec(self, name: str) -> ModelProfileSpec:
        profile = self.providers.get(name)
        return ModelProfileSpec(
            name=profile.name,
            llm=profile.llm,
            llm_api_key_env=profile.llm_api_key_env,
            embedder=profile.embedder,
            embedder_api_key_env=profile.embedder_api_key_env,
            structured_output_mode=profile.structured_output_mode,
            output_repair_attempts=profile.output_repair_attempts,
            request_timeout=profile.request_timeout,
            max_tokens=profile.max_tokens,
        )

    def public_descriptor(self, name: str) -> dict[str, object]:
        return self.providers.public_descriptor(name)

    def resolve_runtime(self, name: str) -> ResolvedModelProfile:
        profile = self.providers.get(name)
        llm_api_key = self.providers._required_env(profile.llm_api_key_env)
        embedder_api_key = (
            self.providers._required_env(profile.embedder_api_key_env)
            if profile.embedder
            else ""
        )
        return ResolvedModelProfile(
            profile=profile,
            llm_api_key=llm_api_key,
            embedder_api_key=embedder_api_key,
        )
