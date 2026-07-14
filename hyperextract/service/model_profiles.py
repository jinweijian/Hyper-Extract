from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from hyperextract.documents.checkpoint import fingerprint


@dataclass(frozen=True)
class ModelProfileSpec:
    """Public specification of a model profile.

    Contains model addresses and behavioral options plus the *names* of the
    environment variables that hold secret values.  Never carries the secret
    values themselves, so it is safe to surface to the API process.
    """

    name: str
    llm: str
    llm_api_key_env: str
    embedder: str
    embedder_api_key_env: str
    structured_output_mode: str = "text_json"
    output_repair_attempts: int = 1
    request_timeout: int = 900
    max_tokens: int | None = None


@dataclass(frozen=True)
class ResolvedModelProfile:
    """Runtime profile with secret values dereferenced from the environment.

    Only constructed by ``ModelProfileRegistry.resolve_runtime`` which is the
    single entry point used by the Worker process.
    """

    name: str
    llm: str
    llm_api_key: str
    embedder: str
    embedder_api_key: str
    structured_output_mode: str = "text_json"
    output_repair_attempts: int = 1
    request_timeout: int = 900
    max_tokens: int | None = None


class ModelProfileRegistry:
    def __init__(self, path: Path | None = None):
        self.path = path

    def _raw_profiles(self) -> dict:
        if self.path is None:
            raise ValueError(
                "MODEL_PROFILE_FILE_MISSING: HE_SERVICE_MODEL_PROFILES not configured"
            )
        with self.path.open("rb") as handle:
            return tomllib.load(handle).get("profiles", {})

    @staticmethod
    def _required_env(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise ValueError(f"MODEL_PROFILE_ENV_MISSING: {name}")
        return value

    def get_spec(self, name: str) -> ModelProfileSpec:
        raw = self._raw_profiles().get(name)
        if not isinstance(raw, dict):
            raise KeyError(name)
        return ModelProfileSpec(
            name=name,
            llm=str(raw["llm"]),
            llm_api_key_env=str(raw["llm_api_key_env"]),
            embedder=str(raw["embedder"]),
            embedder_api_key_env=str(raw["embedder_api_key_env"]),
            structured_output_mode=str(
                raw.get("structured_output_mode", "text_json")
            ),
            output_repair_attempts=int(raw.get("output_repair_attempts", 1)),
            request_timeout=int(raw.get("request_timeout", 900)),
            max_tokens=int(raw["max_tokens"]) if raw.get("max_tokens") else None,
        )

    def public_descriptor(self, name: str) -> dict[str, object]:
        spec = self.get_spec(name)
        public: dict[str, object] = {
            "name": spec.name,
            "llm": spec.llm,
            "embedder": spec.embedder,
            "structured_output_mode": spec.structured_output_mode,
            "output_repair_attempts": spec.output_repair_attempts,
            "request_timeout": spec.request_timeout,
            "max_tokens": spec.max_tokens,
        }
        public["fingerprint"] = fingerprint(public)
        return public

    def resolve_runtime(self, name: str) -> ResolvedModelProfile:
        spec = self.get_spec(name)
        return ResolvedModelProfile(
            name=spec.name,
            llm=spec.llm,
            llm_api_key=self._required_env(spec.llm_api_key_env),
            embedder=spec.embedder,
            embedder_api_key=self._required_env(spec.embedder_api_key_env),
            structured_output_mode=spec.structured_output_mode,
            output_repair_attempts=spec.output_repair_attempts,
            request_timeout=spec.request_timeout,
            max_tokens=spec.max_tokens,
        )
