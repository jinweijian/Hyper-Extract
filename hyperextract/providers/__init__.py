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
    ProbeResult,
    RejectedItem,
    RecoveryDecision,
    ValidationSummary,
)
from hyperextract.providers.gateway import (
    GatewayExecutionError,
    ModelExecutionGateway,
)
from hyperextract.providers.probe import CapabilityProbe, ProbeStore
from hyperextract.providers.recovery import RecoveryPolicy, RecoveryState
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
    "GatewayExecutionError",
    "ModelCapabilities",
    "ModelMessage",
    "ModelExecutionGateway",
    "ModelProfile",
    "OutputMode",
    "ProfileCapabilities",
    "ProfileConfigurationError",
    "ProbeResult",
    "ProbeStore",
    "CapabilityProbe",
    "RejectedItem",
    "ProfileEmbeddingCapabilities",
    "ProfileRecovery",
    "ProviderRegistry",
    "RecoveryDecision",
    "RecoveryPolicy",
    "RecoveryState",
    "ValidationSummary",
    "load_profile",
    "load_profiles_from_toml",
]
