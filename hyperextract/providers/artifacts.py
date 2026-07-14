from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from hyperextract.documents.checkpoint import atomic_write_json
from hyperextract.providers.contracts import RejectedItem, ValidationSummary


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
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = rejection.model_dump(mode="json")
        payload["raw_item"] = _redact_value(payload.get("raw_item"), self._redactor)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            handle.flush()
        return path


def _safe_id(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value)


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
