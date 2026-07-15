from __future__ import annotations

from hyperextract.providers.adapters.base import (
    AdapterError,
    EmbeddingAdapter,
    GenerationAdapter,
    GenerationAdapterError,
)
from hyperextract.providers.adapters.anthropic import AnthropicAdapter
from hyperextract.providers.adapters.openai_chat import OpenAIChatAdapter
from hyperextract.providers.adapters.openai_embeddings import (
    EmbeddingAdapterError,
    EmbeddingProtocolError,
    OpenAIEmbeddingAdapter,
)

__all__ = [
    "AdapterError",
    "AnthropicAdapter",
    "EmbeddingAdapter",
    "EmbeddingAdapterError",
    "EmbeddingProtocolError",
    "GenerationAdapter",
    "GenerationAdapterError",
    "OpenAIChatAdapter",
    "OpenAIEmbeddingAdapter",
]
