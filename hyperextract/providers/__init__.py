"""Canonical model contracts, capability profiles, and provider registry."""

from __future__ import annotations

from hyperextract.providers.contracts import (
    CanonicalModelFailure,
    EmbeddingCapabilities,
    EmbeddingItemResult,
    EmbeddingRequest,
    EmbeddingResponse,
    GenerationRequest,
    GenerationResponse,
    ModelCapabilities,
    ModelMessage,
    OutputMode,
    ProfileConfigurationError,
    RecoveryDecision,
)
from hyperextract.providers.profiles import (
    ModelProfile,
    ProfileCapabilities,
    ProfileEmbeddingCapabilities,
    ProfileRecovery,
    load_profile,
    load_profiles_from_toml,
)
from hyperextract.providers.registry import ProviderRegistry

__all__ = [
    "CanonicalModelFailure",
    "EmbeddingCapabilities",
    "EmbeddingItemResult",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "GenerationRequest",
    "GenerationResponse",
    "ModelCapabilities",
    "ModelMessage",
    "ModelProfile",
    "OutputMode",
    "ProfileCapabilities",
    "ProfileConfigurationError",
    "ProfileEmbeddingCapabilities",
    "ProfileRecovery",
    "ProviderRegistry",
    "RecoveryDecision",
    "load_profile",
    "load_profiles_from_toml",
]
