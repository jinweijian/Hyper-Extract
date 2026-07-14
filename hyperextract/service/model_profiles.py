from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResolvedModelProfile:
    name: str
    llm: str
    llm_api_key: str
    embedder: str
    embedder_api_key: str
    structured_output_mode: str = "text_json"
    output_repair_attempts: int = 1
    request_timeout: int = 900
    max_tokens: int | None = None

    def public_descriptor(self) -> dict[str, object]:
        value = {
            "name": self.name,
            "llm": self.llm,
            "embedder": self.embedder,
            "structured_output_mode": self.structured_output_mode,
            "output_repair_attempts": self.output_repair_attempts,
            "request_timeout": self.request_timeout,
            "max_tokens": self.max_tokens,
        }
        value["fingerprint"] = hashlib.sha256(
            json.dumps(value, sort_keys=True).encode()
        ).hexdigest()
        return value


class ModelProfileRegistry:
    def __init__(self, path: Path | None = None):
        self.path = path

    def _raw_profiles(self) -> dict:
        if self.path is not None:
            with self.path.open("rb") as handle:
                return tomllib.load(handle).get("profiles", {})
        return {
            "minimax-course-default": {
                "llm": "vllm:{model}@{base_url}",
                "llm_model_env": "MIMIMAX_MODEL",
                "llm_base_url_env": "MIMIMAX_BASE_URL",
                "llm_api_key_env": "MIMIMAX_API_KEY",
                "embedder": "vllm:{model}@{base_url}",
                "embedder_model_env": "EMBEDDING_MODEL",
                "embedder_base_url_env": "EMBEDDING_BASE_URL",
                "embedder_api_key_env": "EMBEDDING_API_KEY",
                "structured_output_mode": "text_json",
                "request_timeout": 900,
            }
        }

    @staticmethod
    def _required_env(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise ValueError(f"MODEL_PROFILE_ENV_MISSING: {name}")
        return value

    def get(self, name: str) -> ResolvedModelProfile:
        raw = self._raw_profiles().get(name)
        if not isinstance(raw, dict):
            raise KeyError(name)
        llm = str(raw["llm"])
        if "{model}" in llm:
            llm = llm.format(
                model=self._required_env(str(raw["llm_model_env"])),
                base_url=self._required_env(str(raw["llm_base_url_env"])),
            )
        embedder = str(raw["embedder"])
        if "{model}" in embedder:
            embedder = embedder.format(
                model=self._required_env(str(raw["embedder_model_env"])),
                base_url=self._required_env(str(raw["embedder_base_url_env"])),
            )
        return ResolvedModelProfile(
            name=name,
            llm=llm,
            llm_api_key=self._required_env(str(raw["llm_api_key_env"])),
            embedder=embedder,
            embedder_api_key=self._required_env(str(raw["embedder_api_key_env"])),
            structured_output_mode=str(raw.get("structured_output_mode", "text_json")),
            output_repair_attempts=int(raw.get("output_repair_attempts", 1)),
            request_timeout=int(raw.get("request_timeout", 900)),
            max_tokens=int(raw["max_tokens"]) if raw.get("max_tokens") else None,
        )

    def public_descriptor(self, name: str) -> dict[str, object]:
        return self.get(name).public_descriptor()
