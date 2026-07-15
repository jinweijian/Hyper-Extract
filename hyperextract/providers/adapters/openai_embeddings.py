from __future__ import annotations

import logging
import time
from typing import Any, Callable, Literal

from hyperextract.providers.adapters.base import AdapterError
from hyperextract.providers.contracts import (
    CanonicalModelFailure,
    EmbeddingCapabilities,
    EmbeddingItemResult,
    EmbeddingRequest,
    EmbeddingResponse,
    ProfileConfigurationError,
)
from hyperextract.providers.failures import canonicalize_provider_error
from hyperextract.providers.scheduling import RateLimitGroupScheduler

logger = logging.getLogger(__name__)

_DEFAULT_MAX_BATCH_ITEMS = 10
_DEFAULT_MAX_INPUT_TOKENS_PER_ITEM = 8191
_PLACEHOLDER_PROBE_INPUT = "."


class EmbeddingProtocolError(ValueError):
    """Raised when the provider breaks position alignment or dimension consistency."""

    def __init__(self, message: str, *, indices: list[int] | None = None) -> None:
        super().__init__(message)
        self.indices = indices or []


class EmbeddingAdapterError(AdapterError):
    """Raised when the underlying provider call fails; carries a CanonicalModelFailure."""


class OpenAIEmbeddingAdapter:
    name = "openai_embeddings"

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None,
        api_key: str,
        capabilities: EmbeddingCapabilities,
        client: Any | None = None,
        max_retries: int = 2,
        encoding: Any | None = None,
        warning_sink: Callable[[dict[str, Any]], None] | None = None,
        usage_sink: Callable[[dict[str, Any]], None] | None = None,
        item_failure_policy: Literal["quarantine", "fail"] = "quarantine",
        scheduler: RateLimitGroupScheduler | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._capabilities = capabilities
        self._max_retries = max_retries
        self._warning_sink = warning_sink
        self._usage_sink = usage_sink
        self._item_failure_policy = item_failure_policy
        self._scheduler = scheduler
        if client is not None:
            self._client = client
        else:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                max_retries=max_retries,
            )
        if encoding is not None:
            self._encoding = encoding
        else:
            import tiktoken

            try:
                self._encoding = tiktoken.encoding_for_model(model)
            except KeyError:
                self._encoding = tiktoken.get_encoding("cl100k_base")

    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        inputs = request.inputs
        n = len(inputs)
        if n == 0:
            return EmbeddingResponse(request_id=request.request_id, items=[])

        if (
            request.dimensions is not None
            and not self._capabilities.supports_dimensions
        ):
            raise ProfileConfigurationError(
                f"Model {self._model!r} does not support the dimensions parameter",
                code="EMBEDDING_DIMENSIONS_UNSUPPORTED",
            )

        blank_indices = [i for i, t in enumerate(inputs) if not t.strip()]
        if blank_indices and self._capabilities.empty_input_policy == "reject":
            raise ProfileConfigurationError(
                f"Empty inputs at indices {blank_indices} rejected by policy",
                code="EMBEDDING_EMPTY_INPUT_REJECTED",
            )

        chunks = self._split_texts(inputs, set(blank_indices))
        batches = self._group_batches(chunks)

        sums: dict[int, list[float]] = {}
        counts: dict[int, int] = {}
        saw_usage = False
        input_tokens_total = 0
        provider_request_id: str | None = None
        validation_warnings: list[str] = []
        expected_dim: int | None = None
        quarantined: dict[int, CanonicalModelFailure] = {}

        for batch in batches:
            successful, failed = self._call_with_split_recovery(
                batch, request.dimensions, request.request_id
            )
            quarantined.update(failed)
            for recovered_batch, response in successful:
                if len(response.data) != len(recovered_batch):
                    raise EmbeddingProtocolError(
                        f"Protocol error: expected {len(recovered_batch)} embeddings, "
                        f"got {len(response.data)} from provider"
                    )
                for position, ((_, orig_idx, _), data_item) in enumerate(
                    zip(recovered_batch, response.data, strict=True)
                ):
                    provider_index = getattr(data_item, "index", position)
                    if provider_index != position:
                        raise EmbeddingProtocolError(
                            "Protocol error: embedding response order does not match "
                            f"the request (position {position}, provider index "
                            f"{provider_index})",
                            indices=[orig_idx],
                        )
                    vec = list(data_item.embedding)
                    if (
                        request.dimensions is not None
                        and len(vec) != request.dimensions
                    ):
                        raise EmbeddingProtocolError(
                            "Protocol error: provider returned vector dimension "
                            f"{len(vec)} instead of requested {request.dimensions}",
                            indices=[orig_idx],
                        )
                    if expected_dim is None:
                        expected_dim = len(vec)
                    elif len(vec) != expected_dim:
                        raise EmbeddingProtocolError(
                            f"Protocol error: inconsistent vector dimensions "
                            f"(expected {expected_dim}, got {len(vec)})"
                        )
                    if orig_idx not in sums:
                        sums[orig_idx] = vec
                        counts[orig_idx] = 1
                    else:
                        running = sums[orig_idx]
                        sums[orig_idx] = [
                            a + b for a, b in zip(running, vec, strict=True)
                        ]
                        counts[orig_idx] += 1
                usage = getattr(response, "usage", None)
                if usage is not None:
                    saw_usage = True
                    prompt_toks = getattr(usage, "prompt_tokens", None)
                    input_toks = getattr(usage, "input_tokens", None)
                    batch_tokens = (
                        prompt_toks if prompt_toks is not None else input_toks
                    )
                    if batch_tokens is not None:
                        input_tokens_total += batch_tokens
                rid = getattr(response, "id", None)
                if rid is not None:
                    provider_request_id = rid

        results: list[list[float] | None] = [None] * n
        dim = expected_dim
        for orig_idx, running in sums.items():
            if orig_idx in quarantined:
                continue
            count = counts[orig_idx]
            mean = [v / count for v in running]
            results[orig_idx] = mean
            dim = len(mean)

        if blank_indices:
            policy = self._capabilities.empty_input_policy
            if policy == "quarantine":
                for i in blank_indices:
                    results[i] = None
            elif policy == "zero_vector":
                if dim is None:
                    probe_resp = self._placeholder_probe(request.request_id)
                    probe_vec = list(probe_resp.data[0].embedding)
                    dim = len(probe_vec)
                zero = [0.0] * dim
                for i in blank_indices:
                    results[i] = list(zero)
                logger.warning(
                    "zero vectors inserted for blank input indices %s",
                    blank_indices,
                )
                warning = (
                    f"zero vectors inserted for blank input indices {blank_indices}"
                )
                validation_warnings.append(warning)
                if self._warning_sink:
                    self._warning_sink(
                        {
                            "request_id": request.request_id,
                            "category": "embedding_zero_vector",
                            "indices": blank_indices,
                            "warning": warning,
                        }
                    )

        if quarantined:
            indices = sorted(quarantined)
            warning = f"embedding inputs quarantined at indices {indices}"
            validation_warnings.append(warning)
            logger.warning("%s", warning)
            if self._warning_sink:
                self._warning_sink(
                    {
                        "request_id": request.request_id,
                        "category": "embedding_item_quarantined",
                        "indices": indices,
                        "reasons": {
                            index: failure.reason
                            for index, failure in quarantined.items()
                        },
                        "warning": warning,
                    }
                )

        items: list[EmbeddingItemResult] = []
        for i in range(n):
            vec = results[i]
            if vec is None:
                items.append(
                    EmbeddingItemResult(
                        input_index=i,
                        vector=None,
                        status="quarantined",
                        error_reason=(
                            quarantined[i].reason if i in quarantined else "empty_input"
                        ),
                    )
                )
            else:
                items.append(
                    EmbeddingItemResult(
                        input_index=i,
                        vector=vec,
                        status="completed",
                    )
                )

        if len(items) != n:
            raise EmbeddingProtocolError(
                f"Protocol error: expected {n} items, got {len(items)}"
            )
        if quarantined and len(quarantined) == n:
            first = quarantined[min(quarantined)]
            raise EmbeddingAdapterError(
                "All embedding inputs failed with item-level provider errors",
                failure=first,
            )

        return EmbeddingResponse(
            request_id=request.request_id,
            items=items,
            input_tokens=input_tokens_total if saw_usage else None,
            provider_request_id=provider_request_id,
            validation_warnings=validation_warnings,
        )

    def _call_with_split_recovery(
        self,
        batch: list[tuple[Any, int, int]],
        dimensions: int | None,
        request_id: str,
    ) -> tuple[
        list[tuple[list[tuple[Any, int, int]], Any]],
        dict[int, CanonicalModelFailure],
    ]:
        try:
            response = self._call_api(batch, dimensions, request_id)
            return [(batch, response)], {}
        except EmbeddingAdapterError as error:
            splittable = (
                error.failure.category == "context_window"
                or error.failure.reason == "invalid_input"
            )
            if not splittable:
                raise
            if len(batch) == 1:
                if self._item_failure_policy == "fail":
                    raise
                return [], {batch[0][1]: error.failure}
            self._emit_usage(
                {
                    "type": "recovery",
                    "operation": "embedding",
                    "request_id": request_id,
                    "action": "split",
                    "reason": error.failure.reason,
                    "failure_category": error.failure.category,
                }
            )
            middle = len(batch) // 2
            left_success, left_failed = self._call_with_split_recovery(
                batch[:middle], dimensions, request_id
            )
            right_success, right_failed = self._call_with_split_recovery(
                batch[middle:], dimensions, request_id
            )
            return (
                [*left_success, *right_success],
                {**left_failed, **right_failed},
            )

    def _split_texts(
        self, inputs: list[str], blank_indices: set[int]
    ) -> list[tuple[Any, int, int]]:
        chunks: list[tuple[Any, int, int]] = []
        max_tokens = (
            self._capabilities.max_input_tokens_per_item
            or _DEFAULT_MAX_INPUT_TOKENS_PER_ITEM
        )
        if self._capabilities.max_batch_tokens is not None:
            max_tokens = min(max_tokens, self._capabilities.max_batch_tokens)
        accepts_token_ids = self._capabilities.accepts_token_ids
        for i, text in enumerate(inputs):
            if i in blank_indices:
                continue
            tokens = self._encoding.encode(text)
            if len(tokens) <= max_tokens:
                payload = tokens if accepts_token_ids else text
                chunks.append((payload, i, len(tokens)))
            else:
                for j in range(0, len(tokens), max_tokens):
                    chunk_tokens = tokens[j : j + max_tokens]
                    payload = (
                        chunk_tokens
                        if accepts_token_ids
                        else self._encoding.decode(chunk_tokens)
                    )
                    chunks.append((payload, i, len(chunk_tokens)))
        return chunks

    def _group_batches(
        self, chunks: list[tuple[Any, int, int]]
    ) -> list[list[tuple[Any, int, int]]]:
        max_items = self._capabilities.max_batch_items or _DEFAULT_MAX_BATCH_ITEMS
        max_tokens = self._capabilities.max_batch_tokens
        batches: list[list[tuple[Any, int, int]]] = []
        current: list[tuple[Any, int, int]] = []
        current_tokens = 0
        for chunk in chunks:
            chunk_tokens = chunk[2]
            would_exceed_items = len(current) >= max_items
            would_exceed_tokens = (
                max_tokens is not None
                and bool(current)
                and current_tokens + chunk_tokens > max_tokens
            )
            if would_exceed_items or would_exceed_tokens:
                batches.append(current)
                current = []
                current_tokens = 0
            current.append(chunk)
            current_tokens += chunk_tokens
        if current:
            batches.append(current)
        return batches

    def _call_api(
        self,
        batch: list[tuple[Any, int, int]],
        dimensions: int | None,
        request_id: str,
    ) -> Any:
        payload = [chunk[0] for chunk in batch]
        kwargs: dict[str, Any] = {"input": payload, "model": self._model}
        if dimensions is not None and self._capabilities.supports_dimensions:
            kwargs["dimensions"] = dimensions
        return self._scheduled_create(
            kwargs,
            estimated_tokens=sum(chunk[2] for chunk in batch),
            request_id=request_id,
        )

    def _placeholder_probe(self, request_id: str) -> Any:
        return self._scheduled_create(
            {"input": _PLACEHOLDER_PROBE_INPUT, "model": self._model},
            estimated_tokens=1,
            request_id=request_id,
        )

    def _scheduled_create(
        self,
        kwargs: dict[str, Any],
        *,
        estimated_tokens: int,
        request_id: str,
    ) -> Any:
        started = time.monotonic()
        response = None
        try:
            if self._scheduler is None:
                response = self._client.embeddings.create(**kwargs)
            else:
                with self._scheduler.slot(estimated_tokens=estimated_tokens):
                    response = self._client.embeddings.create(**kwargs)
                self._scheduler.succeeded()
        except Exception as error:
            wrapped = self._wrap_error(error, request_id=request_id)
            self._emit_usage(
                {
                    "type": "attempt",
                    "operation": "embedding",
                    "request_id": request_id,
                    "estimated_input_tokens": estimated_tokens,
                    "input_tokens": None,
                    "provider_request_id": None,
                    "elapsed_seconds": time.monotonic() - started,
                    "error": wrapped,
                }
            )
            if self._scheduler is not None and wrapped.failure.category.startswith(
                "rate_limit."
            ):
                self._scheduler.rate_limited(wrapped.failure.retry_after_seconds or 1.0)
            raise wrapped from error
        self._emit_usage(
            {
                "type": "attempt",
                "operation": "embedding",
                "request_id": request_id,
                "estimated_input_tokens": estimated_tokens,
                "input_tokens": _response_input_tokens(response),
                "provider_request_id": getattr(response, "id", None),
                "elapsed_seconds": time.monotonic() - started,
                "error": None,
            }
        )
        return response

    def set_usage_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        """Attach per-physical-request accounting after pipeline construction."""
        self._usage_sink = sink

    def _emit_usage(self, event: dict[str, Any]) -> None:
        if self._usage_sink is not None:
            self._usage_sink(event)

    def _wrap_error(
        self, error: Exception, *, request_id: str
    ) -> EmbeddingAdapterError:
        failure = canonicalize_provider_error(
            error,
            request_id=request_id,
            secret_values=(self._api_key,),
        )
        return EmbeddingAdapterError(str(failure.raw_message), failure=failure)


def _response_input_tokens(response: Any) -> int | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    input_tokens = getattr(usage, "input_tokens", None)
    value = prompt_tokens if prompt_tokens is not None else input_tokens
    return int(value) if value is not None else None
