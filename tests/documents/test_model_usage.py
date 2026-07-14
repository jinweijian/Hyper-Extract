import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from pydantic import BaseModel

from hyperextract.documents.model_usage import ModelUsageTracker
from hyperextract.documents.structured_output import StructuredOutputInvoker


class _Result(BaseModel):
    name: str


def test_structured_invoker_persists_provider_usage_and_operation(tmp_path):
    model = SimpleNamespace(
        invoke=lambda _input: AIMessage(
            content='{"name":"alpha"}',
            usage_metadata={
                "input_tokens": 120,
                "output_tokens": 20,
                "total_tokens": 140,
            },
        )
    )
    path = tmp_path / "model-usage.json"
    tracker = ModelUsageTracker()
    tracker.attach(path)

    result = StructuredOutputInvoker(
        model,
        _Result,
        mode="text_json",
        repair_attempts=0,
        usage_tracker=tracker,
        operation="local_chunk",
    ).invoke("prompt")

    assert result.name == "alpha"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["total_calls"] == 1
    assert persisted["successful_calls"] == 1
    assert persisted["input_tokens"] == 120
    assert persisted["output_tokens"] == 20
    assert persisted["by_operation"]["local_chunk"]["calls"] == 1
    assert persisted["by_mode"]["text_json"]["calls"] == 1


def test_usage_tracker_resumes_existing_counts(tmp_path):
    path = tmp_path / "model-usage.json"
    first = ModelUsageTracker()
    first.attach(path)
    first.record(
        operation="dedup",
        mode="text_json",
        prompt="prompt",
        schema={},
        response=AIMessage(content="{}"),
        elapsed_seconds=1.0,
    )
    second = ModelUsageTracker()
    second.attach(path, resume=True)
    second.record(
        operation="global_edges",
        mode="text_json",
        prompt="prompt",
        schema={},
        response=AIMessage(content="{}"),
        elapsed_seconds=2.0,
        error=RuntimeError("failed"),
    )

    snapshot = second.snapshot()
    assert snapshot["total_calls"] == 2
    assert snapshot["successful_calls"] == 1
    assert snapshot["failed_calls"] == 1
    assert snapshot["elapsed_seconds"] == 3.0
