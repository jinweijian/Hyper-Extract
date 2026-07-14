from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import AuthenticationError

from hyperextract.providers.adapters.openai_embeddings import (
    EmbeddingAdapterError,
    EmbeddingProtocolError,
    OpenAIEmbeddingAdapter,
)
from hyperextract.providers.contracts import (
    EmbeddingCapabilities,
    EmbeddingRequest,
    ProfileConfigurationError,
)


class FakeEncoding:
    """Round-trip encoding where each character maps to its ord value."""

    def encode(self, text: str) -> list[int]:
        return [ord(c) for c in text]

    def decode(self, tokens: list[int]) -> str:
        return "".join(chr(t) for t in tokens)


class FakeEmbeddings:
    def __init__(self, client: FakeOpenAIClient) -> None:
        self._client = client

    def create(self, *, input: Any, model: str, dimensions: int | None = None):
        return self._client._handle_create(input=input, model=model, dimensions=dimensions)


class FakeOpenAIClient:
    """Records embeddings.create calls and returns deterministic vectors."""

    def __init__(
        self,
        *,
        default_dim: int = 4,
        short_by: int = 0,
        inconsistent_dims: bool = False,
        raise_error: Exception | None = None,
    ) -> None:
        self.embeddings = FakeEmbeddings(self)
        self.calls: list[dict[str, Any]] = []
        self.default_dim = default_dim
        self.short_by = short_by
        self.inconsistent_dims = inconsistent_dims
        self.raise_error = raise_error

    def _handle_create(self, *, input: Any, model: str, dimensions: int | None):
        if self.raise_error is not None:
            raise self.raise_error
        self.calls.append({"input": input, "model": model, "dimensions": dimensions})
        if isinstance(input, str):
            inputs = [input]
        elif isinstance(input, list):
            inputs = input
        else:
            inputs = [input]
        dim = dimensions or self.default_dim
        data = []
        for position, inp in enumerate(inputs):
            data.append(SimpleNamespace(embedding=self._make_vector(inp, dim, position)))
        if self.short_by > 0:
            data = data[: max(0, len(data) - self.short_by)]
        usage = SimpleNamespace(prompt_tokens=5, input_tokens=5)
        return SimpleNamespace(data=data, usage=usage, id="req_abc")

    def _make_vector(self, inp: Any, dim: int, position: int) -> list[float]:
        if self.inconsistent_dims:
            return [float(position)] * max(1, dim - (position % 2))
        if isinstance(inp, str):
            base = [float(len(inp)), float(sum(ord(c) for c in inp) % 100)]
        else:
            base = [float(len(inp)), float(sum(inp) % 100)]
        while len(base) < dim:
            base.append(0.0)
        return base[:dim]


def _make_adapter(
    *,
    capabilities: EmbeddingCapabilities | None = None,
    client: FakeOpenAIClient | None = None,
    encoding: Any | None = None,
    api_key: str = "sk-test",
) -> OpenAIEmbeddingAdapter:
    return OpenAIEmbeddingAdapter(
        model="text-embedding-3-small",
        base_url=None,
        api_key=api_key,
        capabilities=capabilities or EmbeddingCapabilities(transport="openai_embeddings"),
        client=client if client is not None else FakeOpenAIClient(),
        encoding=encoding,
    )


def test_happy_path_three_inputs_aligned():
    fake = FakeOpenAIClient(default_dim=4)
    adapter = _make_adapter(client=fake)
    response = adapter.embed(EmbeddingRequest(inputs=["a", "b", "c"], request_id="r1"))
    assert len(response.items) == 3
    for i, item in enumerate(response.items):
        assert item.input_index == i
        assert item.status == "completed"
        assert item.vector is not None
        assert len(item.vector) == 4
    assert response.input_tokens == 5
    assert response.provider_request_id == "req_abc"


def test_batch_splitting_25_inputs_max_batch_items_10():
    fake = FakeOpenAIClient(default_dim=4)
    adapter = _make_adapter(
        capabilities=EmbeddingCapabilities(
            transport="openai_embeddings", max_batch_items=10
        ),
        client=fake,
    )
    inputs = [f"item-{i}" for i in range(25)]
    response = adapter.embed(EmbeddingRequest(inputs=inputs, request_id="r1"))
    assert len(response.items) == 25
    for i, item in enumerate(response.items):
        assert item.input_index == i
        assert item.status == "completed"
        assert item.vector is not None
    assert len(fake.calls) == 3
    assert len(fake.calls[0]["input"]) == 10
    assert len(fake.calls[1]["input"]) == 10
    assert len(fake.calls[2]["input"]) == 5


def test_multi_chunk_mean_vector():
    fake = FakeOpenAIClient(default_dim=4)
    encoding = FakeEncoding()
    adapter = _make_adapter(
        capabilities=EmbeddingCapabilities(
            transport="openai_embeddings", max_input_tokens_per_item=4
        ),
        client=fake,
        encoding=encoding,
    )
    response = adapter.embed(EmbeddingRequest(inputs=["abcdefghij"], request_id="r1"))
    assert len(response.items) == 1
    item = response.items[0]
    assert item.input_index == 0
    assert item.status == "completed"
    assert item.vector is not None
    chunk_vectors = [
        fake._make_vector("abcd", 4, 0),
        fake._make_vector("efgh", 4, 0),
        fake._make_vector("ij", 4, 0),
    ]
    expected = [sum(v[i] for v in chunk_vectors) / len(chunk_vectors) for i in range(4)]
    assert item.vector == pytest.approx(expected)
    assert len(fake.calls) == 1
    assert len(fake.calls[0]["input"]) == 3


def test_empty_input_policy_reject_raises_and_no_api_call():
    fake = FakeOpenAIClient(default_dim=4)
    adapter = _make_adapter(
        capabilities=EmbeddingCapabilities(
            transport="openai_embeddings", empty_input_policy="reject"
        ),
        client=fake,
    )
    with pytest.raises((EmbeddingProtocolError, ProfileConfigurationError)) as excinfo:
        adapter.embed(EmbeddingRequest(inputs=["a", "", "b"], request_id="r1"))
    assert len(fake.calls) == 0
    if isinstance(excinfo.value, ProfileConfigurationError):
        assert excinfo.value.code == "EMBEDDING_EMPTY_INPUT_REJECTED"


def test_empty_input_policy_quarantine_preserves_alignment():
    fake = FakeOpenAIClient(default_dim=4)
    adapter = _make_adapter(
        capabilities=EmbeddingCapabilities(
            transport="openai_embeddings", empty_input_policy="quarantine"
        ),
        client=fake,
    )
    response = adapter.embed(EmbeddingRequest(inputs=["a", "", "b"], request_id="r1"))
    assert len(response.items) == 3
    assert response.items[0].input_index == 0
    assert response.items[0].status == "completed"
    assert response.items[0].vector is not None
    assert response.items[1].input_index == 1
    assert response.items[1].status == "quarantined"
    assert response.items[1].vector is None
    assert response.items[1].error_reason == "empty_input"
    assert response.items[2].input_index == 2
    assert response.items[2].status == "completed"
    assert response.items[2].vector is not None


def test_empty_input_policy_zero_vector_logs_warning_and_learns_dim(caplog):
    fake = FakeOpenAIClient(default_dim=4)
    adapter = _make_adapter(
        capabilities=EmbeddingCapabilities(
            transport="openai_embeddings", empty_input_policy="zero_vector"
        ),
        client=fake,
    )
    with caplog.at_level(
        logging.WARNING,
        logger="hyperextract.providers.adapters.openai_embeddings",
    ):
        response = adapter.embed(EmbeddingRequest(inputs=["", ""], request_id="r1"))
    assert len(response.items) == 2
    for i, item in enumerate(response.items):
        assert item.input_index == i
        assert item.status == "completed"
        assert item.vector is not None
        assert all(v == 0.0 for v in item.vector)
        assert len(item.vector) == 4
    assert len(fake.calls) == 1
    assert fake.calls[0]["input"] == "."
    assert any("zero" in rec.message.lower() for rec in caplog.records)


def test_protocol_error_on_count_mismatch():
    fake = FakeOpenAIClient(default_dim=4, short_by=1)
    adapter = _make_adapter(client=fake)
    with pytest.raises(EmbeddingProtocolError):
        adapter.embed(EmbeddingRequest(inputs=["a", "b", "c"], request_id="r1"))


def test_protocol_error_on_dimension_mismatch():
    fake = FakeOpenAIClient(default_dim=4, inconsistent_dims=True)
    adapter = _make_adapter(client=fake)
    with pytest.raises(EmbeddingProtocolError):
        adapter.embed(EmbeddingRequest(inputs=["a", "b"], request_id="r1"))


def test_dimensions_unsupported_raises_before_api_call():
    fake = FakeOpenAIClient(default_dim=4)
    adapter = _make_adapter(
        capabilities=EmbeddingCapabilities(
            transport="openai_embeddings", supports_dimensions=False
        ),
        client=fake,
    )
    with pytest.raises(ProfileConfigurationError) as excinfo:
        adapter.embed(
            EmbeddingRequest(inputs=["a"], request_id="r1", dimensions=1024)
        )
    assert excinfo.value.code == "EMBEDDING_DIMENSIONS_UNSUPPORTED"
    assert len(fake.calls) == 0


def test_dimensions_supported_passes_through_to_api():
    fake = FakeOpenAIClient(default_dim=4)
    adapter = _make_adapter(
        capabilities=EmbeddingCapabilities(
            transport="openai_embeddings", supports_dimensions=True
        ),
        client=fake,
    )
    adapter.embed(EmbeddingRequest(inputs=["a"], request_id="r1", dimensions=1024))
    assert len(fake.calls) == 1
    assert fake.calls[0]["dimensions"] == 1024


def test_error_wrapping_authentication_redacts_api_key():
    api_key = "sk-test-123"
    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    response = httpx.Response(status_code=401, request=request)
    err = AuthenticationError(
        f"Unauthorized for key {api_key}", response=response, body=None
    )
    fake = FakeOpenAIClient(raise_error=err)
    adapter = _make_adapter(
        capabilities=EmbeddingCapabilities(transport="openai_embeddings"),
        client=fake,
        api_key=api_key,
    )
    with pytest.raises(EmbeddingAdapterError) as excinfo:
        adapter.embed(EmbeddingRequest(inputs=["a"], request_id="r1"))
    failure = excinfo.value.failure
    assert failure.category == "authentication"
    assert failure.http_status == 401
    assert api_key not in (failure.raw_message or "")


def test_token_ids_path_sends_integer_lists():
    fake = FakeOpenAIClient(default_dim=4)
    encoding = FakeEncoding()
    adapter = _make_adapter(
        capabilities=EmbeddingCapabilities(
            transport="openai_embeddings", accepts_token_ids=True
        ),
        client=fake,
        encoding=encoding,
    )
    adapter.embed(EmbeddingRequest(inputs=["ab", "cd"], request_id="r1"))
    assert len(fake.calls) == 1
    sent = fake.calls[0]["input"]
    assert isinstance(sent, list)
    assert len(sent) == 2
    for item in sent:
        assert isinstance(item, list)
        assert all(isinstance(tok, int) for tok in item)
