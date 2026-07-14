"""Crash-safe, provider-neutral model call and token accounting."""

from __future__ import annotations

import json
import math
import re
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .checkpoint import atomic_write_json


def _estimate_tokens(value: Any) -> int:
    if isinstance(value, BaseModel):
        text = value.model_dump_json()
    elif hasattr(value, "to_messages"):
        text = "\n".join(
            str(getattr(message, "content", message)) for message in value.to_messages()
        )
    elif isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value or "")
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    return max(0, cjk + math.ceil(max(0, len(text) - cjk) / 4))


def _usage(response: Any) -> tuple[int | None, int | None]:
    direct_input = getattr(response, "input_tokens", None)
    direct_output = getattr(response, "output_tokens", None)
    if direct_input is not None or direct_output is not None:
        return (
            int(direct_input) if direct_input is not None else None,
            int(direct_output) if direct_output is not None else None,
        )
    usage = getattr(response, "usage_metadata", None) or {}
    metadata = getattr(response, "response_metadata", None) or {}
    token_usage = metadata.get("token_usage") or metadata.get("usage") or {}
    input_tokens = (
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or token_usage.get("input_tokens")
        or token_usage.get("prompt_tokens")
    )
    output_tokens = (
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or token_usage.get("output_tokens")
        or token_usage.get("completion_tokens")
    )
    return (
        int(input_tokens) if input_tokens is not None else None,
        int(output_tokens) if output_tokens is not None else None,
    )


def _empty() -> dict[str, Any]:
    return {
        "schema_name": "HyperExtractModelUsage",
        "schema_version": "1.0",
        "total_calls": 0,
        "successful_calls": 0,
        "failed_calls": 0,
        "repair_calls": 0,
        "provider_reported_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "elapsed_seconds": 0.0,
        "by_operation": {},
        "by_mode": {},
    }


class ModelUsageTracker:
    """Accumulate usage and atomically persist after every attempted request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._data = _empty()

    def attach(
        self, path: str | Path, *, resume: bool = True, force: bool = False
    ) -> None:
        target = Path(path)
        with self._lock:
            self._path = target
            if resume and not force and target.exists():
                loaded = json.loads(target.read_text(encoding="utf-8"))
                self._data = loaded if isinstance(loaded, dict) else _empty()
            else:
                self._data = _empty()
                atomic_write_json(target, self._data)

    def record(
        self,
        *,
        operation: str,
        mode: str,
        prompt: Any,
        schema: Any,
        response: Any,
        elapsed_seconds: float,
        error: Exception | None = None,
        repair: bool = False,
    ) -> None:
        provider_input, provider_output = _usage(response)
        input_tokens = provider_input
        if input_tokens is None:
            input_tokens = _estimate_tokens(prompt) + _estimate_tokens(schema)
        output_tokens = provider_output
        if output_tokens is None:
            output_tokens = _estimate_tokens(response)

        with self._lock:
            self._data["total_calls"] += 1
            self._data["successful_calls"] += int(error is None)
            self._data["failed_calls"] += int(error is not None)
            self._data["repair_calls"] += int(repair)
            self._data["provider_reported_calls"] += int(
                provider_input is not None or provider_output is not None
            )
            self._data["input_tokens"] += input_tokens
            self._data["output_tokens"] += output_tokens
            self._data["elapsed_seconds"] = round(
                self._data["elapsed_seconds"] + elapsed_seconds, 6
            )
            for dimension, key in (
                ("by_operation", operation),
                ("by_mode", mode),
            ):
                bucket = self._data[dimension].setdefault(
                    key,
                    {
                        "calls": 0,
                        "successful_calls": 0,
                        "failed_calls": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "elapsed_seconds": 0.0,
                    },
                )
                bucket["calls"] += 1
                bucket["successful_calls"] += int(error is None)
                bucket["failed_calls"] += int(error is not None)
                bucket["input_tokens"] += input_tokens
                bucket["output_tokens"] += output_tokens
                bucket["elapsed_seconds"] = round(
                    bucket["elapsed_seconds"] + elapsed_seconds, 6
                )
            if self._path is not None:
                atomic_write_json(self._path, self._data)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))
