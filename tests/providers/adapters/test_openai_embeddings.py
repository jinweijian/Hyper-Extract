from __future__ import annotations

import logging
from contextlib import contextmanager
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
        return self._client._handle_create(
            input=input, model=model, dimensions=dimensions
        )


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
            data.append(
                SimpleNamespace(embedding=self._make_vector(inp, dim, position))
            )
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


class SelectiveBadInputClient(FakeOpenAIClient):
    def _handle_create(self, *, input: Any, model: str, dimensions: int | None):
        values = input if isinstance(input, list) else [input]
        self.calls.append({"input": input, "model": model, "dimensions": dimensions})
        if "bad" in values:
            error = ValueError("one embedding input is invalid")
            error.status_code = 400
            raise error
        dim = dimensions or self.default_dim
        return SimpleNamespace(
            data=[
                SimpleNamespace(embedding=self._make_vector(value, dim, position))
                for position, value in enumerate(values)
            ],
            usage=SimpleNamespace(prompt_tokens=len(values), input_tokens=len(values)),
            id="req_split",
        )


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
        capabilities=capabilities
        or EmbeddingCapabilities(transport="openai_embeddings"),
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


def test_single_chunk_never_exceeds_max_batch_tokens():
    fake = FakeOpenAIClient(default_dim=4)
    adapter = _make_adapter(
        capabilities=EmbeddingCapabilities(
            transport="openai_embeddings",
            max_input_tokens_per_item=10,
            max_batch_tokens=4,
        ),
        client=fake,
        encoding=FakeEncoding(),
    )

    adapter.embed(EmbeddingRequest(inputs=["abcdef"], request_id="r1"))

    assert [len(value) for call in fake.calls for value in call["input"]] == [4, 2]
    assert all(
        sum(len(value) for value in call["input"]) <= 4 for call in fake.calls
    )


def test_scheduler_counts_each_physical_batch_request():
    class CountingScheduler:
        calls = 0
        successes = 0

        @contextmanager
        def slot(self, *, estimated_tokens=0):
            self.calls += 1
            assert estimated_tokens > 0
            yield

        def succeeded(self):
            self.successes += 1

        def rate_limited(self, delay_seconds):
            raise AssertionError(delay_seconds)

    fake = FakeOpenAIClient(default_dim=4)
    scheduler = CountingScheduler()
    adapter = OpenAIEmbeddingAdapter(
        model="text-embedding-3-small",
        base_url=None,
        api_key="sk-test",
        capabilities=EmbeddingCapabilities(
            transport="openai_embeddings", max_batch_items=2
        ),
        client=fake,
        encoding=FakeEncoding(),
        scheduler=scheduler,
    )

    adapter.embed(EmbeddingRequest(inputs=["a", "b", "c", "d", "e"], request_id="r1"))

    assert scheduler.calls == 3
    assert scheduler.successes == 3


def test_failed_batch_is_split_and_only_bad_item_is_quarantined():
    fake = SelectiveBadInputClient(default_dim=4)
    adapter = _make_adapter(client=fake)

    response = adapter.embed(
        EmbeddingRequest(inputs=["good-1", "bad", "good-2"], request_id="r1")
    )

    assert [item.status for item in response.items] == [
        "completed",
        "quarantined",
        "completed",
    ]
    assert response.items[1].vector is None
    assert response.items[1].error_reason == "invalid_input"
    assert response.validation_warnings == [
        "embedding inputs quarantined at indices [1]"
    ]
    assert fake.calls[0]["input"] == ["good-1", "bad", "good-2"]
    assert any(call["input"] == ["bad"] for call in fake.calls)


def test_usage_sink_observes_physical_calls_and_split_recovery():
    events = []
    adapter = OpenAIEmbeddingAdapter(
        model="text-embedding-3-small",
        base_url=None,
        api_key="sk-test",
        capabilities=EmbeddingCapabilities(transport="openai_embeddings"),
        client=SelectiveBadInputClient(default_dim=4),
        encoding=FakeEncoding(),
        usage_sink=events.append,
    )

    adapter.embed(EmbeddingRequest(inputs=["good", "bad"], request_id="r1"))

    attempts = [event for event in events if event["type"] == "attempt"]
    recoveries = [event for event in events if event["type"] == "recovery"]
    assert len(attempts) == 3
    assert [event["error"] is None for event in attempts] == [False, True, False]
    assert recoveries[0]["action"] == "split"


def test_generic_bad_request_is_not_split_or_quarantined():
    error = ValueError("invalid model configuration")
    error.status_code = 400
    fake = FakeOpenAIClient(raise_error=error)
    adapter = _make_adapter(client=fake)

    with pytest.raises(EmbeddingAdapterError) as raised:
        adapter.embed(EmbeddingRequest(inputs=["one", "two", "three"], request_id="r1"))

    assert raised.value.failure.reason == "bad_request"


def test_all_item_local_embedding_failures_fail_the_request():
    adapter = _make_adapter(client=SelectiveBadInputClient(default_dim=4))

    with pytest.raises(EmbeddingAdapterError) as raised:
        adapter.embed(EmbeddingRequest(inputs=["bad"], request_id="r1"))

    assert raised.value.failure.reason == "invalid_input"


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
        adapter.embed(EmbeddingRequest(inputs=["a"], request_id="r1", dimensions=1024))
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
