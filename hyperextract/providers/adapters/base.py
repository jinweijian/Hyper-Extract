from __future__ import annotations

from typing import Protocol, runtime_checkable

from hyperextract.providers.contracts import (
    EmbeddingRequest,
    EmbeddingResponse,
    GenerationRequest,
    GenerationResponse,
)


@runtime_checkable
class GenerationAdapter(Protocol):
    name: str

    def invoke(self, request: GenerationRequest) -> GenerationResponse: ...


@runtime_checkable
class EmbeddingAdapter(Protocol):
    name: str

    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse: ...
