"""Provider-neutral structured output invocation and defensive JSON parsing."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Literal, TypeVar

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, ValidationError

from .model_errors import (
    OutputTruncatedError,
    OutputValidationError,
    UnsupportedModelCapabilityError,
    classify_model_error,
)
from .model_usage import ModelUsageTracker


T = TypeVar("T", bound=BaseModel)
OutputMode = Literal["auto", "native", "tool", "json_object", "text_json"]
_THINKING = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.IGNORECASE | re.DOTALL)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def _balanced_json(text: str) -> str | None:
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
    raise OutputTruncatedError("Model returned truncated JSON")


def extract_json_value(text: str) -> Any:
    """Extract the first complete JSON object or array from a chat response."""
    cleaned = _THINKING.sub("", text).strip()
    fence = _FENCE.search(cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    candidate = _balanced_json(cleaned)
    if candidate is None:
        raise OutputValidationError("Model response contains no JSON object or array")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as error:
        raise OutputValidationError(
            f"Invalid JSON response: {error}", original=error
        ) from error


def _message_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    content = (
        response.content
        if isinstance(response, AIMessage)
        else getattr(response, "content", None)
    )
    response_metadata = getattr(response, "response_metadata", {}) or {}
    finish_reason = str(
        response_metadata.get("finish_reason")
        or response_metadata.get("stop_reason")
        or ""
    ).lower()
    if finish_reason in {"length", "max_tokens", "max_output_tokens"}:
        raise OutputTruncatedError(
            f"Model output was truncated: finish_reason={finish_reason}"
        )
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {
                None,
                "text",
                "output_text",
            }:
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(filter(None, parts))
    raise OutputValidationError("Model response does not contain final text content")


class StructuredOutputInvoker:
    """Invoke one schema through native or plain-JSON provider capabilities."""

    def __init__(
        self,
        model: Any,
        schema: type[T],
        *,
        mode: OutputMode = "auto",
        repair_attempts: int = 1,
        raw_response_sink: Callable[[str], None] | None = None,
        usage_tracker: ModelUsageTracker | None = None,
        operation: str = "structured_output",
    ) -> None:
        self.model = model
        self.schema = schema
        self.mode = mode
        self.repair_attempts = max(0, repair_attempts)
        self.raw_response_sink = raw_response_sink
        self.usage_tracker = usage_tracker
        self.operation = operation
        self.last_mode: OutputMode | None = None

    def as_runnable(self) -> RunnableLambda:
        return RunnableLambda(self.invoke)

    def _validate(self, response: Any) -> T:
        if isinstance(response, self.schema):
            return response
        if isinstance(response, dict):
            value = response
        else:
            text = _message_text(response)
            if self.raw_response_sink:
                self.raw_response_sink(text)
            value = extract_json_value(text)
        try:
            return self.schema.model_validate(value)
        except ValidationError as error:
            raise OutputValidationError(
                f"Structured output validation failed: {error}", original=error
            ) from error

    def _plain_prompt(self, prompt: Any, *, repair: str = "") -> Any:
        schema = json.dumps(self.schema.model_json_schema(), ensure_ascii=False)
        instruction = (
            "Return only one JSON value matching this JSON Schema. "
            "Do not include reasoning, XML tags, or Markdown fences.\n"
            f"JSON Schema: {schema}"
        )
        if repair:
            instruction += f"\nThe previous response was invalid. Repair it:\n{repair}"
        if hasattr(prompt, "to_messages"):
            return [*prompt.to_messages(), HumanMessage(content=instruction)]
        if isinstance(prompt, list):
            return [*prompt, HumanMessage(content=instruction)]
        return f"{prompt}\n\n{instruction}"

    def _invoke_mode(self, prompt: Any, mode: OutputMode) -> T:
        started = time.monotonic()
        response: Any = None
        try:
            if mode == "native":
                response = self.model.with_structured_output(
                    self.schema, method="json_schema"
                ).invoke(prompt)
            elif mode == "tool":
                response = self.model.with_structured_output(
                    self.schema, method="function_calling"
                ).invoke(prompt)
            elif mode == "json_object":
                response = self.model.bind(
                    response_format={"type": "json_object"}
                ).invoke(self._plain_prompt(prompt))
            else:
                response = self.model.invoke(self._plain_prompt(prompt))
            result = self._validate(response)
            self.last_mode = mode
            if self.usage_tracker:
                self.usage_tracker.record(
                    operation=self.operation,
                    mode=mode,
                    prompt=prompt,
                    schema=self.schema.model_json_schema(),
                    response=response,
                    elapsed_seconds=time.monotonic() - started,
                )
            return result
        except (OutputTruncatedError, OutputValidationError) as error:
            if self.usage_tracker:
                self.usage_tracker.record(
                    operation=self.operation,
                    mode=mode,
                    prompt=prompt,
                    schema=self.schema.model_json_schema(),
                    response=response,
                    elapsed_seconds=time.monotonic() - started,
                    error=error,
                )
            raise
        except Exception as error:
            if self.usage_tracker:
                self.usage_tracker.record(
                    operation=self.operation,
                    mode=mode,
                    prompt=prompt,
                    schema=self.schema.model_json_schema(),
                    response=response,
                    elapsed_seconds=time.monotonic() - started,
                    error=error,
                )
            raise classify_model_error(error) from error

    def invoke(self, prompt: Any) -> T:
        modes: tuple[OutputMode, ...] = (
            ("native", "tool", "text_json") if self.mode == "auto" else (self.mode,)
        )
        last_error: Exception | None = None
        for mode in modes:
            try:
                return self._invoke_mode(prompt, mode)
            except UnsupportedModelCapabilityError as error:
                last_error = error
                continue
            except OutputTruncatedError:
                # Repeating the same prompt cannot recover a provider length cutoff.
                # The caller must reduce the requested batch or input size.
                raise
            except OutputValidationError as error:
                last_error = error
                if self.repair_attempts <= 0:
                    raise
                try:
                    raw = str(error)
                    repair_prompt = self._plain_prompt(prompt, repair=raw)
                    started = time.monotonic()
                    response = None
                    try:
                        response = self.model.invoke(repair_prompt)
                        result = self._validate(response)
                    except Exception as repair_error:
                        if self.usage_tracker:
                            self.usage_tracker.record(
                                operation=self.operation,
                                mode="text_json",
                                prompt=repair_prompt,
                                schema=self.schema.model_json_schema(),
                                response=response,
                                elapsed_seconds=time.monotonic() - started,
                                error=repair_error,
                                repair=True,
                            )
                        raise
                    if self.usage_tracker:
                        self.usage_tracker.record(
                            operation=self.operation,
                            mode="text_json",
                            prompt=repair_prompt,
                            schema=self.schema.model_json_schema(),
                            response=response,
                            elapsed_seconds=time.monotonic() - started,
                            repair=True,
                        )
                    self.last_mode = "text_json"
                    return result
                except Exception as repair_error:
                    if isinstance(
                        repair_error, (OutputValidationError, OutputTruncatedError)
                    ):
                        raise repair_error
                    raise classify_model_error(repair_error) from repair_error
        if last_error is not None:
            raise last_error
        raise OutputValidationError("No structured output mode was attempted")
