"""Provider-neutral structured output invocation and item-level quarantine."""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable, Literal, TypeVar, get_args, get_origin

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, TypeAdapter, ValidationError

from hyperextract.providers.contracts import (
    GenerationRequest,
    ModelMessage,
    RejectedItem,
    ValidationSummary,
)
from hyperextract.providers.gateway import ModelExecutionGateway
from hyperextract.providers.artifacts import ModelArtifactStore, redact_sensitive_text
from hyperextract.providers.normalization import (
    NormalizationError,
    TruncatedJSONError,
    extract_json_value as _extract_json_value,
    normalize_generation_payload,
)
from hyperextract.documents.checkpoint import fingerprint

from .model_errors import (
    OutputTruncatedError,
    OutputValidationError,
    UnsupportedModelCapabilityError,
    classify_model_error,
)
from .model_usage import ModelUsageTracker

T = TypeVar("T", bound=BaseModel)
OutputMode = Literal["auto", "native", "tool", "json_object", "text_json"]


def extract_json_value(text: str) -> Any:
    """Compatibility export for the shared response normalizer."""
    try:
        return _extract_json_value(text)
    except TruncatedJSONError as error:
        raise OutputTruncatedError(str(error), original=error) from error
    except NormalizationError as error:
        raise OutputValidationError(
            str(error), original=error, raw_response=text
        ) from error


def _message_text(response: Any) -> str:
    metadata = getattr(response, "response_metadata", {}) or {}
    finish_reason = str(
        metadata.get("finish_reason") or metadata.get("stop_reason") or ""
    ).lower()
    if not finish_reason:
        finish_reason = str(getattr(response, "finish_reason", "") or "").lower()
    if finish_reason in {"length", "max_tokens", "max_output_tokens"}:
        raise OutputTruncatedError(
            f"Model output was truncated: finish_reason={finish_reason}"
        )
    try:
        return normalize_generation_payload(response).final_text
    except NormalizationError as error:
        raise OutputValidationError(str(error), original=error) from error


class StructuredOutputInvoker:
    """Invoke one schema with bounded repair and auditable partial success."""

    def __init__(
        self,
        model: Any,
        schema: type[T],
        *,
        mode: OutputMode = "auto",
        repair_attempts: int = 1,
        validation_retry_attempts: int = 0,
        raw_response_sink: Callable[[str], None] | None = None,
        usage_tracker: ModelUsageTracker | None = None,
        operation: str = "structured_output",
        gateway: ModelExecutionGateway | None = None,
        invalid_item_policy: Literal["quarantine", "fail"] = "quarantine",
        invalid_item_ratio_threshold: float = 0.2,
        rejection_sink: Callable[[RejectedItem], None] | None = None,
        validation_sink: Callable[[ValidationSummary], None] | None = None,
        request_metadata: (dict[str, str] | Callable[[], dict[str, str]] | None) = None,
        artifact_store: ModelArtifactStore | None = None,
    ) -> None:
        self.model = model
        self.schema = schema
        self.mode = mode
        self.repair_attempts = max(0, repair_attempts)
        self.validation_retry_attempts = max(0, validation_retry_attempts)
        self.raw_response_sink = raw_response_sink
        self.usage_tracker = usage_tracker
        self.operation = operation
        self.gateway = gateway
        self.invalid_item_policy = invalid_item_policy
        self.invalid_item_ratio_threshold = invalid_item_ratio_threshold
        self.rejection_sink = rejection_sink
        self.validation_sink = validation_sink
        self._request_metadata_source = request_metadata or {}
        self.artifact_store = artifact_store
        self._local = threading.local()
        self.last_mode: OutputMode | None = None
        self.last_validation_summary: ValidationSummary | None = None
        self._request_id = ""
        self._last_raw_text = ""

    @property
    def last_mode(self) -> OutputMode | None:
        return getattr(self._local, "last_mode", None)

    @last_mode.setter
    def last_mode(self, value: OutputMode | None) -> None:
        self._local.last_mode = value

    @property
    def last_validation_summary(self) -> ValidationSummary | None:
        return getattr(self._local, "last_validation_summary", None)

    @last_validation_summary.setter
    def last_validation_summary(self, value: ValidationSummary | None) -> None:
        self._local.last_validation_summary = value

    @property
    def request_metadata(self) -> dict[str, str]:
        active = getattr(self._local, "request_metadata", None)
        if active is not None:
            return active
        source = self._request_metadata_source
        return dict(source() if callable(source) else source)

    @property
    def _request_id(self) -> str:
        return getattr(self._local, "request_id", "")

    @_request_id.setter
    def _request_id(self, value: str) -> None:
        self._local.request_id = value

    @property
    def _last_raw_text(self) -> str:
        return getattr(self._local, "last_raw_text", "")

    @_last_raw_text.setter
    def _last_raw_text(self, value: str) -> None:
        self._local.last_raw_text = value

    @property
    def _repair_attempt(self) -> int:
        return getattr(self._local, "repair_attempt", 0)

    @_repair_attempt.setter
    def _repair_attempt(self, value: int) -> None:
        self._local.repair_attempt = value

    @property
    def _validation_attempt(self) -> int:
        return getattr(self._local, "validation_attempt", 0)

    @_validation_attempt.setter
    def _validation_attempt(self, value: int) -> None:
        self._local.validation_attempt = value

    def as_runnable(self) -> RunnableLambda:
        return RunnableLambda(self.invoke)

    def _validate(self, response: Any) -> T:
        if isinstance(response, self.schema):
            self._emit_summary(valid=1, rejected=[])
            return response
        if isinstance(response, dict):
            value = response
            self._last_raw_text = json.dumps(response, ensure_ascii=False, default=str)
        else:
            text = _message_text(response)
            self._last_raw_text = text
            if self.raw_response_sink:
                self.raw_response_sink(text)
            if self.artifact_store:
                self.artifact_store.save_raw_response(self._request_id, text)
            value = extract_json_value(text)

        result, rejections = self._validate_with_quarantine(value)
        if rejections:
            total = self._count_list_items(value)
            ratio = len(rejections) / max(1, total)
            if (
                self.invalid_item_policy == "fail"
                or ratio > self.invalid_item_ratio_threshold
            ):
                can_repair = (
                    self.invalid_item_policy != "fail" and self.repair_attempts > 0
                )
                if not can_repair:
                    rejections = self._persist_rejections(rejections, "failed")
                self._emit_summary(
                    valid=max(0, total - len(rejections)),
                    rejected=rejections,
                    failed=True,
                )
                raise OutputValidationError(
                    f"Structured output rejected {len(rejections)}/{total} list items",
                    raw_response=self._last_raw_text,
                    rejections=rejections,
                )
            rejections = self._persist_rejections(rejections, "quarantined")
        item_count = self._count_list_items(value)
        self._emit_summary(
            valid=max(0, item_count - len(rejections)) if item_count else 1,
            rejected=rejections,
        )
        return result

    def _validate_with_quarantine(self, value: Any) -> tuple[T, list[RejectedItem]]:
        if not isinstance(value, dict):
            try:
                return self.schema.model_validate(value), []
            except ValidationError as error:
                raise OutputValidationError(
                    f"Structured output validation failed: {error}",
                    original=error,
                    raw_response=self._last_raw_text,
                ) from error

        cleaned = dict(value)
        rejections: list[RejectedItem] = []
        for field_name, field in self.schema.model_fields.items():
            annotation = field.annotation
            if get_origin(annotation) is not list or field_name not in cleaned:
                continue
            raw_items = cleaned[field_name]
            if not isinstance(raw_items, list):
                continue
            item_type = get_args(annotation)[0]
            adapter = TypeAdapter(item_type)
            valid_items = []
            for index, raw_item in enumerate(raw_items):
                try:
                    valid_items.append(adapter.validate_python(raw_item))
                except ValidationError as error:
                    rejections.append(
                        self._rejection(field_name, index, raw_item, error)
                    )
            cleaned[field_name] = valid_items
        try:
            return self.schema.model_validate(cleaned), rejections
        except ValidationError as error:
            raise OutputValidationError(
                f"Structured output validation failed: {error}",
                original=error,
                raw_response=self._last_raw_text,
                rejections=rejections,
            ) from error

    def _rejection(
        self,
        field_name: str,
        index: int,
        raw_item: Any,
        error: ValidationError,
    ) -> RejectedItem:
        first = error.errors()[0] if error.errors() else {}
        suffix = ".".join(str(part) for part in first.get("loc", ()))
        schema_path = f"{field_name}.{index}" + (f".{suffix}" if suffix else "")
        rejection_id = fingerprint(
            {
                "request_id": self._request_id,
                "stage": self.operation,
                "chunk_id": self.request_metadata.get("chunk_id"),
                "batch_id": self.request_metadata.get("batch_id"),
                "schema_path": schema_path,
                "raw_item": raw_item,
            }
        )
        return RejectedItem(
            rejection_id=rejection_id,
            request_id=self._request_id,
            stage=self.operation,
            chunk_id=self.request_metadata.get("chunk_id"),
            batch_id=self.request_metadata.get("batch_id"),
            schema_path=schema_path,
            raw_item=raw_item,
            validation_attempt=self._validation_attempt,
            repair_attempt=self._repair_attempt,
            error=str(first.get("msg") or error),
            profile_fingerprint=self.request_metadata.get("profile_fingerprint"),
            model_fingerprint=self.request_metadata.get("model_fingerprint"),
            prompt_fingerprint=self.request_metadata.get("prompt_fingerprint"),
        )

    def _persist_rejections(
        self,
        rejections: list[RejectedItem],
        action: Literal["quarantined", "repaired", "failed"],
    ) -> list[RejectedItem]:
        final = [
            rejection.model_copy(update={"action": action})
            for rejection in _deduplicate_rejections(rejections)
        ]
        for rejection in final:
            if self.rejection_sink:
                self.rejection_sink(rejection)
            if self.artifact_store:
                self.artifact_store.save_rejection(rejection)
        return final

    def _emit_summary(
        self,
        *,
        valid: int,
        rejected: list[RejectedItem],
        failed: bool = False,
    ) -> None:
        affected: dict[str, int] = {}
        unknown: list[str] = []
        for rejection in rejected:
            item = rejection.raw_item if isinstance(rejection.raw_item, dict) else {}
            for key in ("source", "target"):
                endpoint = item.get(key) if isinstance(item, dict) else None
                if endpoint:
                    affected[str(endpoint)] = affected.get(str(endpoint), 0) + 1
                elif key in rejection.schema_path:
                    unknown.append(f"{rejection.schema_path}: missing {key}")
        total = valid + len(rejected)
        summary = ValidationSummary(
            request_id=self._request_id,
            status=(
                "failed"
                if failed
                else "completed_with_rejections"
                if rejected
                else "completed"
            ),
            valid_items=valid,
            rejected_items=len(rejected),
            rejected_ratio=len(rejected) / max(1, total),
            affected_endpoints=affected,
            unknown_endpoints=unknown,
            connectivity_warnings=(
                [
                    "Quarantined relationships may introduce isolated nodes or "
                    "additional connected components"
                ]
                if rejected
                else []
            ),
            graph_connectivity_incomplete=bool(rejected),
        )
        self.last_validation_summary = summary
        if self.validation_sink:
            self.validation_sink(summary)
        if self.artifact_store:
            self.artifact_store.save_validation(summary)

    def _count_list_items(self, value: Any) -> int:
        if not isinstance(value, dict):
            return 0
        return sum(
            len(item)
            for name, item in value.items()
            if name in self.schema.model_fields and isinstance(item, list)
        )

    def _plain_prompt(self, prompt: Any, *, repair: str = "") -> Any:
        schema = json.dumps(self.schema.model_json_schema(), ensure_ascii=False)
        instruction = (
            "Return only one JSON value matching this JSON Schema. "
            "Do not include reasoning, XML tags, or Markdown fences.\n"
            f"JSON Schema: {schema}"
        )
        if repair:
            instruction += (
                "\nThe previous response was invalid. Repair the exact JSON below; "
                "do not invent missing factual endpoints.\n"
                f"Invalid response: {repair}"
            )
        if hasattr(prompt, "to_messages"):
            return [*prompt.to_messages(), HumanMessage(content=instruction)]
        if isinstance(prompt, list):
            return [*prompt, HumanMessage(content=instruction)]
        return f"{prompt}\n\n{instruction}"

    def _invoke_mode(self, prompt: Any, mode: OutputMode) -> T:
        started = time.monotonic()
        response: Any = None
        gateway_attempts_recorded = False
        try:
            if self.gateway is not None:
                try:
                    response = self.gateway.invoke(
                        GenerationRequest(
                            operation=self.operation,
                            messages=_to_model_messages(self._plain_prompt(prompt)),
                            output_schema=self.schema.model_json_schema(),
                            structured_output=True,
                            structured_output_mode=mode,
                            request_id=self._request_id,
                            metadata={
                                **self.request_metadata,
                                "schema_name": self.schema.__name__,
                            },
                        )
                    )
                finally:
                    gateway_attempts_recorded = self._record_gateway_attempts(prompt)
                self.last_mode = (
                    self.gateway.last_trace.modes[-1]
                    if self.gateway.last_trace.modes
                    else mode
                )
            elif mode == "native":
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
            self.last_mode = self.last_mode or mode
            if not gateway_attempts_recorded:
                self._record(prompt, response, self.last_mode, started)
            return result
        except (OutputTruncatedError, OutputValidationError) as error:
            if not gateway_attempts_recorded:
                self._record(prompt, response, mode, started, error=error)
            raise
        except Exception as error:
            if not gateway_attempts_recorded:
                self._record(prompt, response, mode, started, error=error)
            raise classify_model_error(error) from error

    def _record(
        self,
        prompt: Any,
        response: Any,
        mode: str,
        started: float,
        *,
        error: Exception | None = None,
        repair: bool = False,
    ) -> None:
        if self.usage_tracker:
            self.usage_tracker.record(
                operation=self.operation,
                mode=mode,
                prompt=prompt,
                schema=self.schema.model_json_schema(),
                response=response,
                elapsed_seconds=time.monotonic() - started,
                error=error,
                repair=repair,
            )

    def _record_gateway_attempts(self, prompt: Any, *, repair: bool = False) -> bool:
        if self.gateway is None:
            return False
        attempts = self.gateway.last_trace.attempts
        if self.usage_tracker is not None:
            for attempt in attempts:
                self.usage_tracker.record(
                    operation=self.operation,
                    mode=attempt.mode,
                    prompt=prompt,
                    schema=self.schema.model_json_schema(),
                    response=attempt.response,
                    elapsed_seconds=attempt.elapsed_seconds,
                    error=attempt.error,
                    repair=repair,
                )
            for recovery in self.gateway.last_trace.recoveries:
                self.usage_tracker.record_recovery(
                    operation=self.operation,
                    mode=recovery.mode,
                    action=recovery.decision.action,
                    reason=recovery.decision.reason,
                    request_id=recovery.failure.request_id,
                    failure_category=recovery.failure.category,
                )
        return True

    def invoke(self, prompt: Any) -> T:
        source = self._request_metadata_source
        self._local.request_metadata = dict(source() if callable(source) else source)
        self._request_id = self.request_metadata.get("request_id") or uuid.uuid4().hex
        retry_rejections: list[RejectedItem] = []
        for validation_attempt in range(self.validation_retry_attempts + 1):
            self._validation_attempt = validation_attempt
            self._repair_attempt = 0
            try:
                result = self._invoke_once(prompt)
            except OutputValidationError as error:
                retry_rejections.extend(error.rejections)
                can_retry = (
                    validation_attempt < self.validation_retry_attempts
                    and not (error.rejections and self.invalid_item_policy == "fail")
                )
                if can_retry:
                    continue
                if retry_rejections:
                    error.rejections = self._persist_rejections(
                        retry_rejections, "failed"
                    )
                raise
            if retry_rejections:
                self._persist_rejections(retry_rejections, "repaired")
            return result
        raise OutputValidationError("Structured output validation retries exhausted")

    def _invoke_once(self, prompt: Any) -> T:
        modes: tuple[OutputMode, ...] = (
            ("auto",)
            if self.mode == "auto" and self.gateway is not None
            else (
                ("native", "tool", "text_json") if self.mode == "auto" else (self.mode,)
            )
        )
        last_error: Exception | None = None
        for mode in modes:
            try:
                return self._invoke_mode(prompt, mode)
            except UnsupportedModelCapabilityError as error:
                last_error = error
                continue
            except OutputTruncatedError:
                raise
            except OutputValidationError as error:
                last_error = error
                repair_rejections = list(error.rejections)
                invalid = redact_sensitive_text(
                    error.raw_response or self._last_raw_text or str(error)
                )
                repair_budget = (
                    0
                    if error.rejections and self.invalid_item_policy == "fail"
                    else self.repair_attempts
                )
                for repair_attempt in range(1, repair_budget + 1):
                    self._repair_attempt = repair_attempt
                    repair_prompt = self._plain_prompt(prompt, repair=invalid)
                    started = time.monotonic()
                    response = None
                    repair_mode: OutputMode = (
                        "auto" if self.gateway is not None else "text_json"
                    )
                    gateway_attempts_recorded = False
                    try:
                        if self.gateway is not None:
                            try:
                                response = self.gateway.invoke(
                                    GenerationRequest(
                                        operation=f"{self.operation}.repair",
                                        messages=_to_model_messages(repair_prompt),
                                        output_schema=self.schema.model_json_schema(),
                                        structured_output=True,
                                        structured_output_mode=repair_mode,
                                        request_id=self._request_id,
                                        metadata={
                                            **self.request_metadata,
                                            "schema_name": self.schema.__name__,
                                            "repair": "true",
                                        },
                                    )
                                )
                            finally:
                                gateway_attempts_recorded = (
                                    self._record_gateway_attempts(
                                        repair_prompt, repair=True
                                    )
                                )
                        else:
                            response = self.model.invoke(repair_prompt)
                        result = self._validate(response)
                        if repair_rejections:
                            self._persist_rejections(repair_rejections, "repaired")
                        if not gateway_attempts_recorded:
                            self._record(
                                repair_prompt,
                                response,
                                repair_mode,
                                started,
                                repair=True,
                            )
                        self.last_mode = (
                            self.gateway.last_trace.modes[-1]
                            if self.gateway is not None
                            and self.gateway.last_trace.modes
                            else repair_mode
                        )
                        return result
                    except OutputTruncatedError:
                        raise
                    except OutputValidationError as repair_error:
                        last_error = repair_error
                        repair_rejections.extend(repair_error.rejections)
                        invalid = redact_sensitive_text(
                            repair_error.raw_response
                            or self._last_raw_text
                            or str(repair_error)
                        )
                        if not gateway_attempts_recorded:
                            self._record(
                                repair_prompt,
                                response,
                                repair_mode,
                                started,
                                error=repair_error,
                                repair=True,
                            )
                        continue
                    except Exception as repair_error:
                        classified = classify_model_error(repair_error)
                        if not gateway_attempts_recorded:
                            self._record(
                                repair_prompt,
                                response,
                                repair_mode,
                                started,
                                error=classified,
                                repair=True,
                            )
                        raise classified from repair_error
                if isinstance(last_error, OutputValidationError):
                    last_error.rejections = _deduplicate_rejections(repair_rejections)
                raise last_error
        if last_error is not None:
            raise last_error
        raise OutputValidationError("No structured output mode was attempted")


def _deduplicate_rejections(rejections: list[RejectedItem]) -> list[RejectedItem]:
    unique: dict[str, RejectedItem] = {}
    for rejection in rejections:
        unique[rejection.rejection_id] = rejection
    return list(unique.values())


def _to_model_messages(prompt: Any) -> list[ModelMessage]:
    if hasattr(prompt, "to_messages"):
        prompt = prompt.to_messages()
    if not isinstance(prompt, list):
        return [ModelMessage(role="user", content=str(prompt))]
    result: list[ModelMessage] = []
    for message in prompt:
        role_name = getattr(message, "type", None) or getattr(message, "role", "user")
        role = {"human": "user", "ai": "assistant"}.get(role_name, role_name)
        if role not in {"system", "user", "assistant"}:
            role = "user"
        result.append(
            ModelMessage(role=role, content=str(getattr(message, "content", message)))
        )
    return result
