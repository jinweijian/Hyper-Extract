from __future__ import annotations

import logging
from typing import Any

from hyperextract.providers.contracts import (
    CanonicalModelFailure,
    EmbeddingCapabilities,
    EmbeddingItemResult,
    EmbeddingRequest,
    EmbeddingResponse,
    ProfileConfigurationError,
)

logger = logging.getLogger(__name__)

_DEFAULT_MAX_BATCH_ITEMS = 10
_DEFAULT_MAX_INPUT_TOKENS_PER_ITEM = 8191
_PLACEHOLDER_PROBE_INPUT = "."


class EmbeddingProtocolError(ValueError):
    """Raised when the provider breaks position alignment or dimension consistency."""

    def __init__(self, message: str, *, indices: list[int] | None = None) -> None:
        super().__init__(message)
        self.indices = indices or []


class EmbeddingAdapterError(RuntimeError):
    """Raised when the underlying provider call fails; carries a CanonicalModelFailure."""

    def __init__(self, message: str, *, failure: CanonicalModelFailure) -> None:
        super().__init__(message)
        self.failure = failure


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
    ) -> None:
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._capabilities = capabilities
        self._max_retries = max_retries
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
        expected_dim: int | None = None

        for batch in batches:
            response = self._call_api(batch, request.dimensions, request.request_id)
            if len(response.data) != len(batch):
                raise EmbeddingProtocolError(
                    f"Protocol error: expected {len(batch)} embeddings, "
                    f"got {len(response.data)} from provider"
                )
            for (payload, orig_idx, _), data_item in zip(
                batch, response.data, strict=True
            ):
                vec = list(data_item.embedding)
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
                    sums[orig_idx] = [a + b for a, b in zip(running, vec, strict=True)]
                    counts[orig_idx] += 1
            usage = getattr(response, "usage", None)
            if usage is not None:
                saw_usage = True
                prompt_toks = getattr(usage, "prompt_tokens", None)
                input_toks = getattr(usage, "input_tokens", None)
                batch_tokens = prompt_toks if prompt_toks is not None else input_toks
                if batch_tokens is not None:
                    input_tokens_total += batch_tokens
            rid = getattr(response, "id", None)
            if rid is not None:
                provider_request_id = rid

        results: list[list[float] | None] = [None] * n
        dim = expected_dim
        for orig_idx, running in sums.items():
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

        items: list[EmbeddingItemResult] = []
        for i in range(n):
            vec = results[i]
            if vec is None:
                items.append(
                    EmbeddingItemResult(
                        input_index=i,
                        vector=None,
                        status="quarantined",
                        error_reason="empty_input",
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

        return EmbeddingResponse(
            request_id=request.request_id,
            items=items,
            input_tokens=input_tokens_total if saw_usage else None,
            provider_request_id=provider_request_id,
        )

    def _split_texts(
        self, inputs: list[str], blank_indices: set[int]
    ) -> list[tuple[Any, int, int]]:
        chunks: list[tuple[Any, int, int]] = []
        max_tokens = (
            self._capabilities.max_input_tokens_per_item
            or _DEFAULT_MAX_INPUT_TOKENS_PER_ITEM
        )
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
        try:
            return self._client.embeddings.create(**kwargs)
        except Exception as e:
            raise self._wrap_error(e, request_id=request_id) from e

    def _placeholder_probe(self, request_id: str) -> Any:
        try:
            return self._client.embeddings.create(
                input=_PLACEHOLDER_PROBE_INPUT, model=self._model
            )
        except Exception as e:
            raise self._wrap_error(e, request_id=request_id) from e

    def _wrap_error(
        self, error: Exception, *, request_id: str
    ) -> EmbeddingAdapterError:
        from openai import APIConnectionError, APITimeoutError

        status = getattr(error, "status_code", None)
        if isinstance(error, (APITimeoutError, APIConnectionError)):
            category = "transient"
            reason = "connection_or_timeout"
        elif status is None:
            category = "unknown"
            reason = "no_status"
        elif status in (401, 403):
            category = "authentication"
            reason = "auth_failed"
        elif status == 429:
            category = "rate_limit.requests"
            reason = "rate_limited"
        elif status >= 500:
            category = "transient"
            reason = "server_error"
        elif status == 400:
            category = "protocol"
            reason = "bad_request"
        else:
            category = "unknown"
            reason = f"unhandled_status_{status}"

        raw = str(error)
        if self._api_key and self._api_key in raw:
            raw = raw.replace(self._api_key, "[REDACTED]")

        failure = CanonicalModelFailure(
            request_id=request_id,
            category=category,
            reason=reason,
            http_status=status,
            raw_message=raw,
        )
        return EmbeddingAdapterError(raw, failure=failure)
