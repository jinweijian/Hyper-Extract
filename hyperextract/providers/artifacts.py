from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Callable

from hyperextract.documents.checkpoint import atomic_write_json, atomic_write_text
from hyperextract.providers.contracts import (
    EmbeddingResponse,
    RejectedItem,
    ValidationSummary,
)


class ModelArtifactStore:
    """Write redacted raw, validation, and rejection evidence for one run."""

    def __init__(
        self,
        root: str | Path,
        *,
        redactor: Callable[[str], str] | None = None,
    ) -> None:
        self.root = Path(root)
        self._redactor = redactor or redact_sensitive_text
        self._lock = threading.Lock()

    def save_raw_response(self, request_id: str, text: str) -> Path:
        path = self.root / "raw-responses" / f"{_safe_id(request_id)}.json"
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                existing = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError):
                existing = {}
        responses = list(existing.get("responses") or [])
        responses.append({"final_text": self._redactor(text)})
        atomic_write_json(path, {"request_id": request_id, "responses": responses})
        return path

    def save_validation(self, summary: ValidationSummary) -> Path:
        path = self.root / "validation" / f"{_safe_id(summary.request_id)}.json"
        atomic_write_json(path, summary.model_dump(mode="json"))
        return path

    def save_rejection(self, rejection: RejectedItem) -> Path:
        path = self.root / "rejections" / f"{_safe_id(rejection.request_id)}.jsonl"
        payload = rejection.model_dump(mode="json")
        payload["raw_item"] = _redact_value(payload.get("raw_item"), self._redactor)
        entries: list[dict[str, Any]] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    entries.append(item)
        identity = _rejection_identity(payload)
        entries = [item for item in entries if _rejection_identity(item) != identity]
        entries.append(payload)
        atomic_write_text(
            path,
            "".join(
                json.dumps(item, ensure_ascii=False, default=str) + "\n"
                for item in entries
            ),
        )
        return path

    def save_embedding_response(self, response: EmbeddingResponse) -> Path | None:
        """Persist embedding quarantine evidence without storing vector payloads."""
        quarantined = [
            {
                "input_index": item.input_index,
                "status": item.status,
                "error_reason": item.error_reason,
            }
            for item in response.items
            if item.status == "quarantined"
        ]
        if not quarantined and not response.validation_warnings:
            return None
        path = (
            self.root / "embedding-rejections" / f"{_safe_id(response.request_id)}.json"
        )
        atomic_write_json(
            path,
            {
                "request_id": response.request_id,
                "provider_request_id": response.provider_request_id,
                "input_tokens": response.input_tokens,
                "quarantined_items": quarantined,
                "validation_warnings": [
                    self._redactor(value) for value in response.validation_warnings
                ],
            },
        )
        return path

    def save_gateway_event(self, event: dict[str, Any]) -> Path:
        """Append a redacted recovery decision to the run diagnostics."""
        path = self.root / "diagnostics" / "model-gateway-events.jsonl"
        payload = _redact_value(event, self._redactor)
        with self._lock:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            atomic_write_text(
                path,
                existing + json.dumps(payload, ensure_ascii=False, default=str) + "\n",
            )
        return path


def _safe_id(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value)


def _rejection_identity(value: dict[str, Any]) -> tuple[Any, ...]:
    rejection_id = value.get("rejection_id")
    if rejection_id:
        return (rejection_id,)
    return (
        value.get("request_id"),
        value.get("stage"),
        value.get("chunk_id"),
        value.get("batch_id"),
        value.get("schema_path"),
        json.dumps(value.get("raw_item"), sort_keys=True, default=str),
    )


def _redact_value(value: Any, redactor: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return redactor(value)
    if isinstance(value, list):
        return [_redact_value(item, redactor) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item, redactor) for key, item in value.items()}
    return value


def redact_sensitive_text(value: str) -> str:
    value = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,}\]]+",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", value)
    value = re.sub(
        r'(?i)(api[_-]?key["\']?\s*[:=]\s*["\']?)[^"\'\s,}]+',
        r"\1[REDACTED]",
        value,
    )
    return value
