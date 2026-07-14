from __future__ import annotations

from typing import Protocol, runtime_checkable

from hyperextract.providers.contracts import (
    CanonicalModelFailure,
    EmbeddingRequest,
    EmbeddingResponse,
    GenerationRequest,
    GenerationResponse,
)


class AdapterError(RuntimeError):
    """Provider failure normalized at the adapter boundary."""

    def __init__(self, message: str, *, failure: CanonicalModelFailure) -> None:
        super().__init__(message)
        self.failure = failure


class GenerationAdapterError(AdapterError):
    """Generation provider failure carrying a canonical failure."""


@runtime_checkable
class GenerationAdapter(Protocol):
    name: str

    def invoke(self, request: GenerationRequest) -> GenerationResponse: ...


@runtime_checkable
class EmbeddingAdapter(Protocol):
    name: str

    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse: ...
