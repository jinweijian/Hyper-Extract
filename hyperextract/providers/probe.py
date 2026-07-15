from __future__ import annotations

import json
import os
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
        configured_root = os.environ.get("HE_PROBE_ROOT", "").strip()
        self.root = (
            Path(root)
            if root is not None
            else Path(configured_root)
            if configured_root
            else Path.home() / ".he" / "probes"
        )

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

    def invalidate(self, profile_fingerprint: str) -> None:
        """Remove stale successful evidence before a replacement probe starts."""
        try:
            self.path_for(profile_fingerprint).unlink(missing_ok=True)
        except OSError:
            # A failed invalidation must be visible to the caller; otherwise an old
            # successful result could continue authorizing production traffic.
            raise


class CapabilityProbe:
    """Run bounded, low-cost conformance checks against configured adapters."""

    def __init__(self, generation_adapter, embedding_adapter=None) -> None:
        self.generation_adapter = generation_adapter
        self.embedding_adapter = embedding_adapter

    def run(self, profile: ModelProfile) -> ProbeResult:
        checks: dict[str, bool] = {}
        observations: dict[str, Any] = {}
        timeout = min(profile.request_timeout, 30)
        optional_parameters = profile.capabilities.supported_parameters | (
            profile.capabilities.omit_if_unsupported
        )
        plain_request = GenerationRequest(
            operation="probe.text",
            messages=[
                ModelMessage(
                    role="user",
                    content="Think briefly, then reply with OK only.",
                )
            ],
            max_output_tokens=16,
            temperature=0 if "temperature" in optional_parameters else None,
            timeout_seconds=timeout,
            request_id="probe-text",
        )
        plain = self._attempt(
            "text_request",
            lambda: self.generation_adapter.invoke(plain_request),
            observations,
        )
        checks["text_response"] = bool(plain and plain.final_text.strip())
        checks["finish_reason"] = bool(plain and plain.finish_reason is not None)
        checks["usage"] = bool(
            plain
            and (plain.input_tokens is not None or plain.output_tokens is not None)
        )
        checks["declared_parameters"] = plain is not None
        reasoning_mode = profile.capabilities.reasoning_content_mode
        checks["thinking_separation"] = reasoning_mode == "none" or bool(
            plain and plain.reasoning_text
        )
        if plain is not None:
            observations["finish_reason"] = plain.finish_reason
            observations["reasoning_separated"] = bool(plain.reasoning_text)

        object_schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        structured = self._structured_request(
            profile,
            operation="probe.structured_object",
            content='Return only this JSON object: {"ok":true}',
            schema=object_schema,
            request_id="probe-structured-object",
            timeout=timeout,
            observations=observations,
        )
        parsed = self._parse(structured, "structured_object", observations)
        checks["structured_object"] = (
            isinstance(parsed, dict) and parsed.get("ok") is True
        )

        list_schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        }
        structured_list = self._structured_request(
            profile,
            operation="probe.structured_list",
            content='Return only: {"items":[{"value":"probe"}]}',
            schema=list_schema,
            request_id="probe-structured-list",
            timeout=timeout,
            observations=observations,
        )
        parsed_list = self._parse(structured_list, "structured_list", observations)
        checks["structured_list"] = bool(
            isinstance(parsed_list, dict)
            and isinstance(parsed_list.get("items"), list)
            and parsed_list["items"]
            and isinstance(parsed_list["items"][0], dict)
            and parsed_list["items"][0].get("value") == "probe"
        )

        self._probe_embeddings(profile, checks, observations)

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

    def _attempt(self, name: str, operation, observations: dict[str, Any]):
        try:
            return operation()
        except Exception as error:
            observations[f"{name}_error"] = type(error).__name__
            code = getattr(error, "code", None)
            if code:
                observations[f"{name}_error_code"] = str(code)
            return None

    def _structured_request(
        self,
        profile: ModelProfile,
        *,
        operation: str,
        content: str,
        schema: dict[str, Any],
        request_id: str,
        timeout: int,
        observations: dict[str, Any],
    ):
        return self._attempt(
            operation,
            lambda: self.generation_adapter.invoke(
                GenerationRequest(
                    operation=operation,
                    messages=[ModelMessage(role="user", content=content)],
                    structured_output=True,
                    structured_output_mode=(
                        profile.capabilities.preferred_structured_output_mode
                    ),
                    output_schema=schema,
                    max_output_tokens=96,
                    timeout_seconds=timeout,
                    request_id=request_id,
                )
            ),
            observations,
        )

    def _parse(self, response, name: str, observations: dict[str, Any]) -> Any:
        if response is None:
            return None
        try:
            return extract_json_value(response.final_text)
        except Exception as error:
            observations[f"{name}_parse_error"] = type(error).__name__
            return None

    def _probe_embeddings(
        self,
        profile: ModelProfile,
        checks: dict[str, bool],
        observations: dict[str, Any],
    ) -> None:
        capabilities = profile.embedding_capabilities
        if capabilities is None:
            return
        if self.embedding_adapter is None:
            checks["embedding_adapter_present"] = False
            return
        primary = self._attempt(
            "embedding_strings",
            lambda: self.embedding_adapter.embed(
                EmbeddingRequest(
                    inputs=["probe alpha", "probe beta"],
                    request_id="probe-embedding-strings",
                )
            ),
            observations,
        )
        completed = (
            [item for item in primary.items if item.status == "completed"]
            if primary is not None
            else []
        )
        checks["embedding_string_inputs"] = len(completed) == 2
        checks["embedding_alignment"] = bool(
            primary and [item.input_index for item in primary.items] == [0, 1]
        )
        dimensions = {len(item.vector or []) for item in completed}
        checks["embedding_dimensions"] = len(dimensions) == 1 and bool(dimensions)
        checks["embedding_token_ids"] = (
            not capabilities.accepts_token_ids or primary is not None
        )
        observations["embedding_dimension"] = next(iter(dimensions), None)

        batch_size = min((capabilities.max_batch_items or 2) + 1, 4)
        batch = self._attempt(
            "embedding_batch",
            lambda: self.embedding_adapter.embed(
                EmbeddingRequest(
                    inputs=[f"probe batch {index}" for index in range(batch_size)],
                    request_id="probe-embedding-batch",
                )
            ),
            observations,
        )
        checks["embedding_batch_limit"] = bool(
            batch
            and [item.input_index for item in batch.items] == list(range(batch_size))
        )

        long_words = min((capabilities.max_input_tokens_per_item or 8) + 1, 32)
        long_result = self._attempt(
            "embedding_long_input",
            lambda: self.embedding_adapter.embed(
                EmbeddingRequest(
                    inputs=["probe " * long_words],
                    request_id="probe-embedding-long",
                )
            ),
            observations,
        )
        checks["embedding_long_input"] = bool(
            long_result
            and len(long_result.items) == 1
            and long_result.items[0].input_index == 0
            and long_result.items[0].status == "completed"
        )

        empty_result = self._attempt(
            "embedding_empty_input",
            lambda: self.embedding_adapter.embed(
                EmbeddingRequest(
                    inputs=["probe", "", "tail"],
                    request_id="probe-embedding-empty",
                )
            ),
            observations,
        )
        if capabilities.empty_input_policy == "reject":
            error_name = observations.get("embedding_empty_input_error")
            error_code = observations.get("embedding_empty_input_error_code")
            checks["embedding_empty_policy"] = (
                error_code == "EMBEDDING_EMPTY_INPUT_REJECTED"
                or error_name == "EmbeddingProtocolError"
            )
        else:
            expected_status = (
                "quarantined"
                if capabilities.empty_input_policy == "quarantine"
                else "completed"
            )
            checks["embedding_empty_policy"] = bool(
                empty_result
                and [item.input_index for item in empty_result.items] == [0, 1, 2]
                and empty_result.items[1].status == expected_status
            )


def ensure_probe_eligibility(
    profile: ModelProfile,
    store: ProbeStore | None = None,
) -> ProbeResult | None:
    store = store or ProbeStore()
    result = store.load(profile.public_fingerprint())
    current_ttl_valid = False
    if result is not None:
        probed_at = result.probed_at
        if probed_at.tzinfo is None:
            probed_at = probed_at.replace(tzinfo=timezone.utc)
        current_ttl_valid = datetime.now(timezone.utc) < probed_at + timedelta(
            hours=profile.probe_ttl_hours
        )
    valid = (
        result is not None
        and not result.expired
        and current_ttl_valid
        and all(result.checks.values())
    )
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
