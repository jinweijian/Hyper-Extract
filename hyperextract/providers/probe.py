from __future__ import annotations

import json
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hyperextract.documents.checkpoint import atomic_write_json, fingerprint
from hyperextract.providers.contracts import (
    EmbeddingRequest,
    GenerationRequest,
    ModelMessage,
    ProbeResult,
    ProfileConfigurationError,
)
from hyperextract.providers.normalization import extract_json_value
from hyperextract.providers.profiles import ModelProfile


class ProbeStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else Path.home() / ".he" / "probes"

    def path_for(self, profile_fingerprint: str) -> Path:
        return self.root / f"{profile_fingerprint}.json"

    def save(self, result: ProbeResult) -> Path:
        path = self.path_for(result.profile_fingerprint)
        atomic_write_json(path, result.model_dump(mode="json"))
        return path

    def load(self, profile_fingerprint: str) -> ProbeResult | None:
        path = self.path_for(profile_fingerprint)
        if not path.exists():
            return None
        try:
            return ProbeResult.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


class CapabilityProbe:
    """Run bounded, low-cost conformance checks against configured adapters."""

    def __init__(self, generation_adapter, embedding_adapter=None) -> None:
        self.generation_adapter = generation_adapter
        self.embedding_adapter = embedding_adapter

    def run(self, profile: ModelProfile) -> ProbeResult:
        checks: dict[str, bool] = {}
        observations: dict[str, Any] = {}
        plain = self.generation_adapter.invoke(
            GenerationRequest(
                operation="probe.text",
                messages=[ModelMessage(role="user", content="Reply with OK only.")],
                max_output_tokens=16,
                request_id="probe-text",
            )
        )
        checks["text_response"] = bool(plain.final_text.strip())
        checks["finish_reason"] = plain.finish_reason is not None
        checks["usage"] = (
            plain.input_tokens is not None or plain.output_tokens is not None
        )
        observations["finish_reason"] = plain.finish_reason
        observations["reasoning_separated"] = bool(plain.reasoning_text)

        structured = self.generation_adapter.invoke(
            GenerationRequest(
                operation="probe.structured",
                messages=[
                    ModelMessage(
                        role="user",
                        content='Return only this JSON object: {"ok":true}',
                    )
                ],
                structured_output=True,
                structured_output_mode=(
                    profile.capabilities.preferred_structured_output_mode
                ),
                output_schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
                max_output_tokens=64,
                request_id="probe-structured",
            )
        )
        parsed = extract_json_value(structured.final_text)
        checks["structured_object"] = (
            isinstance(parsed, dict) and parsed.get("ok") is True
        )

        if self.embedding_adapter is not None:
            embedded = self.embedding_adapter.embed(
                EmbeddingRequest(
                    inputs=["probe alpha", "probe beta"],
                    request_id="probe-embedding",
                )
            )
            completed = [item for item in embedded.items if item.status == "completed"]
            checks["embedding_alignment"] = [
                item.input_index for item in embedded.items
            ] == [0, 1]
            checks["embedding_vectors"] = len(completed) == 2
            dimensions = {len(item.vector or []) for item in completed}
            checks["embedding_dimensions"] = len(dimensions) == 1 and bool(dimensions)
            observations["embedding_dimension"] = next(iter(dimensions), None)

        now = datetime.now(timezone.utc)
        evidence = {
            "profile_fingerprint": profile.public_fingerprint(),
            "checks": checks,
            "observations": observations,
        }
        return ProbeResult(
            profile_fingerprint=profile.public_fingerprint(),
            probe_evidence_hash=fingerprint(evidence),
            checks=checks,
            observations=observations,
            probed_at=now,
            expires_at=now + timedelta(hours=profile.probe_ttl_hours),
        )


def ensure_probe_eligibility(
    profile: ModelProfile,
    store: ProbeStore | None = None,
) -> ProbeResult | None:
    store = store or ProbeStore()
    result = store.load(profile.public_fingerprint())
    valid = result is not None and not result.expired and all(result.checks.values())
    if valid:
        return result
    message = (
        f"Profile {profile.name!r} has no current successful capability probe; "
        "static conservative capabilities will be used"
    )
    if profile.probe_required:
        raise ProfileConfigurationError(message, code="PROBE_REQUIRED")
    warnings.warn(message, RuntimeWarning, stacklevel=2)
    return result


def describe_probe(result: ProbeResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return json.loads(result.model_dump_json())
