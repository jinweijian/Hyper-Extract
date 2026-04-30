"""Client Factory - Create OpenAI LLM and Embedder clients from config."""

import os
import logging
from pathlib import Path
from typing import Any, List, Optional, Tuple

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path.home() / ".he"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.toml"

# Official OpenAI API base URL — only this endpoint accepts pre-tokenized input
OPENAI_API_URL = "https://api.openai.com/v1"


class CompatibleEmbeddings(Embeddings):
    """Embeddings for OpenAI-compatible providers that only accept string input.

    langchain_openai's OpenAIEmbeddings with tiktoken_enabled=True sends
    pre-tokenized integer lists to the API, which OpenAI supports but most
    OpenAI-compatible providers (Ollama, LiteLLM, etc.) do not. This class
    works around that by always sending strings, using tiktoken for chunking
    with a fallback encoding when the model name isn't tiktoken-compatible.
    """

    def __init__(
        self,
        model: str = "text-embedding-ada-002",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        chunk_size: int = 1000,
        max_retries: int = 2,
        **kwargs: Any,
    ):
        from openai import OpenAI

        self._client = OpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            base_url=base_url,
            max_retries=max_retries,
        )
        self._model = model
        self._chunk_size = chunk_size

        # Determine the tiktoken encoding to use for chunking
        import tiktoken
        try:
            self._encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            # Model not recognized by tiktoken; use cl100k_base (used by
            # text-embedding-ada-002, text-embedding-3-small, etc.)
            logger.debug(
                "Model '%s' not recognized by tiktoken, using cl100k_base encoding",
                model,
            )
            self._encoding = tiktoken.get_encoding("cl100k_base")

        # Max tokens per request (8191 is the limit for most OpenAI embedders)
        self._max_tokens = kwargs.get("embedding_ctx_length", 8191)

    def _split_texts(self, texts: List[str]) -> List[Tuple[str, int]]:
        """Split texts into chunks that fit within token limits.

        Returns list of (text_chunk, original_index) tuples.
        """
        chunks: List[Tuple[str, int]] = []
        for i, text in enumerate(texts):
            tokens = self._encoding.encode(text)
            if len(tokens) <= self._max_tokens:
                chunks.append((text, i))
            else:
                # Split into chunks
                for j in range(0, len(tokens), self._max_tokens):
                    chunk_tokens = tokens[j : j + self._max_tokens]
                    chunk_text = self._encoding.decode(chunk_tokens)
                    chunks.append((chunk_text, i))
        return chunks

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []

        chunks = self._split_texts(texts)
        if not chunks:
            return []

        # Group chunks into batches of chunk_size
        all_embeddings: List[Optional[List[float]]] = [None] * len(texts)
        batch: List[Tuple[str, int]] = []

        def _embed_batch(b: List[Tuple[str, int]]) -> None:
            response = self._client.embeddings.create(
                input=[text for text, _ in b],
                model=self._model,
            )
            for (text, orig_idx), emb_data in zip(b, response.data, strict=False):
                if all_embeddings[orig_idx] is None:
                    all_embeddings[orig_idx] = emb_data.embedding
                else:
                    # Average embeddings for multi-chunk texts
                    prev = all_embeddings[orig_idx]
                    curr = emb_data.embedding
                    all_embeddings[orig_idx] = [
                        (a + b) / 2 for a, b in zip(prev, curr, strict=False)
                    ]

        for chunk in chunks:
            batch.append(chunk)
            if len(batch) >= self._chunk_size:
                _embed_batch(batch)
                batch = []

        if batch:
            _embed_batch(batch)

        # Fill in any missing embeddings with empty-string embedding
        missing_indices = [i for i, e in enumerate(all_embeddings) if e is None]
        if missing_indices:
            response = self._client.embeddings.create(
                input="",
                model=self._model,
            )
            default_emb = response.data[0].embedding
            for i in missing_indices:
                all_embeddings[i] = default_emb

        return all_embeddings  # type: ignore[return-value]

    def embed_query(self, text: str) -> List[float]:
        return self.embed_documents([text])[0]


def get_client(config_path: str | Path = None) -> Tuple[BaseChatModel, Embeddings]:
    """Get OpenAI LLM client and Embedder from config.

    Args:
        config_path: Config file path, default ~/.he/config.toml

    Returns:
        (llm_client, embedder) tuple

    Examples:
        >>> from hyperextract.utils import get_client
        >>> llm, emb = get_client()
        >>> # Or with custom config path
        >>> llm, emb = get_client("/path/to/config.toml")
    """
    from hyperextract.cli.config import ConfigManager

    path = Path(config_path) if config_path else DEFAULT_CONFIG_FILE
    manager = ConfigManager(path)

    llm_config = manager.get_llm_config()
    emb_config = manager.get_embedder_config()

    from langchain_openai import ChatOpenAI, OpenAIEmbeddings

    llm_client = ChatOpenAI(
        model=llm_config.model,
        api_key=llm_config.api_key or os.environ.get("OPENAI_API_KEY", ""),
        base_url=llm_config.base_url or None,
        temperature=0,
    )

    # When using a non-OpenAI endpoint (e.g. Ollama, Azure, LiteLLM),
    # use CompatibleEmbeddings which always sends strings (not pre-tokenized
    # integer lists) to the API. Most OpenAI-compatible endpoints don't
    # support tokenized input and will return "invalid input type" errors.
    uses_custom_base_url = (
        emb_config.base_url and emb_config.base_url.rstrip("/") != OPENAI_API_URL
    )

    if uses_custom_base_url:
        embedder = CompatibleEmbeddings(
            model=emb_config.model,
            api_key=emb_config.api_key or os.environ.get("OPENAI_API_KEY", ""),
            base_url=emb_config.base_url,
        )
    else:
        embedder = OpenAIEmbeddings(
            model=emb_config.model,
            api_key=emb_config.api_key or os.environ.get("OPENAI_API_KEY", ""),
            base_url=emb_config.base_url or None,
        )

    return llm_client, embedder
