from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_THINKING = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.IGNORECASE | re.DOTALL)
_TAG_WRAP = re.compile(r"</?think(?:ing)?>", re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


class NormalizationError(ValueError):
    """Response cannot be converted to the provider-neutral contract."""


class TruncatedJSONError(NormalizationError):
    """The response began a JSON value but did not close it."""


@dataclass(frozen=True)
class NormalizedText:
    final_text: str
    reasoning_text: str | None = None


def normalize_generation_payload(
    payload: Any,
    *,
    reasoning_content_mode: str = "none",
) -> NormalizedText:
    """Extract final and reasoning text from strings, messages, or content blocks."""
    if isinstance(payload, str):
        content = payload
        separate_reasoning = None
    elif isinstance(payload, list):
        return _normalize_blocks(payload, reasoning_content_mode)
    elif isinstance(payload, dict):
        content = payload.get("content", payload.get("text", ""))
        separate_reasoning = payload.get("reasoning_content") or payload.get(
            "reasoning"
        )
    else:
        content = getattr(payload, "content", None)
        if content is None:
            content = getattr(payload, "final_text", None)
        separate_reasoning = getattr(payload, "reasoning_content", None)
        if separate_reasoning is None:
            separate_reasoning = getattr(payload, "reasoning_text", None)
        additional = getattr(payload, "additional_kwargs", None) or {}
        if separate_reasoning is None and isinstance(additional, dict):
            separate_reasoning = additional.get("reasoning_content")

    if isinstance(content, list):
        normalized = _normalize_blocks(content, reasoning_content_mode)
        if normalized.reasoning_text is None and separate_reasoning:
            return NormalizedText(normalized.final_text, str(separate_reasoning))
        return normalized
    if content is None:
        raise NormalizationError("Model response does not contain final text content")
    final = str(content)
    if reasoning_content_mode == "inline_tags":
        return _strip_inline_thinking(final)
    if reasoning_content_mode == "separate_field":
        return NormalizedText(
            final, str(separate_reasoning) if separate_reasoning else None
        )
    return NormalizedText(final, None)


def _normalize_blocks(blocks: list[Any], mode: str) -> NormalizedText:
    final_parts: list[str] = []
    reasoning_parts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            final_parts.append(block)
            continue
        if isinstance(block, dict):
            kind = block.get("type")
            getter = block.get
        else:
            kind = getattr(block, "type", None)
            getter = lambda key, default=None: getattr(block, key, default)
        if kind in {None, "text", "output_text"}:
            value = getter("text") or getter("content") or ""
            if value:
                final_parts.append(str(value))
        elif kind in {"thinking", "reasoning"}:
            value = (
                getter("thinking")
                or getter("reasoning")
                or getter("text")
                or getter("content")
                or ""
            )
            if value:
                reasoning_parts.append(str(value))
    final = "\n".join(final_parts)
    if mode == "inline_tags":
        inline = _strip_inline_thinking(final)
        reasoning_parts.insert(0, inline.reasoning_text or "")
        final = inline.final_text
    reasoning = "\n".join(filter(None, reasoning_parts)) if reasoning_parts else None
    return NormalizedText(final, reasoning)


def _strip_inline_thinking(content: str) -> NormalizedText:
    matches = _THINKING.findall(content)
    reasoning = "\n".join(_TAG_WRAP.sub("", item).strip() for item in matches)
    return NormalizedText(_THINKING.sub("", content).strip(), reasoning or None)


def extract_json_value(text: str) -> Any:
    """Return the first complete JSON object/array, ignoring fences and thinking."""
    cleaned = _THINKING.sub("", text).strip()
    fence = _FENCE.search(cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    candidate = balanced_json(cleaned)
    if candidate is None:
        raise NormalizationError("Model response contains no JSON object or array")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as error:
        raise NormalizationError(f"Invalid JSON response: {error}") from error


def balanced_json(text: str) -> str | None:
    start = next((index for index, char in enumerate(text) if char in "[{"), None)
    if start is None:
        return None
    stack: list[str] = []
    in_string = False
    escaped = False
    pairs = {"}": "{", "]": "["}
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            stack.append(char)
        elif char in "}]":
            if not stack or stack[-1] != pairs[char]:
                return None
            stack.pop()
            if not stack:
                return text[start : index + 1]
    raise TruncatedJSONError("Model returned truncated JSON")
