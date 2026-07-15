import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from pydantic import BaseModel
import pytest

from hyperextract.documents.model_errors import OutputValidationError
from hyperextract.documents.model_usage import ModelUsageTracker
from hyperextract.documents.structured_output import StructuredOutputInvoker
from hyperextract.providers.adapters.base import GenerationAdapterError
from hyperextract.providers.artifacts import ModelArtifactStore
from hyperextract.providers.contracts import (
    CanonicalModelFailure,
    GenerationResponse,
)
from hyperextract.providers.gateway import ModelExecutionGateway
from hyperextract.providers.profiles import (
    ModelProfile,
    ProfileCapabilities,
    ProfileRecovery,
)


class Edge(BaseModel):
    source: str
    target: str


class EdgeList(BaseModel):
    items: list[Edge]


class SequenceAdapter:
    name = "sequence"

    def __init__(self, values):
        self.values = list(values)
        self.requests = []

    def invoke(self, request):
        self.requests.append(request.model_copy(deep=True))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _gateway(adapter, *, modes=None):
    declared = modes or ["text_json"]
    profile = ModelProfile(
        name="structured-test",
        llm="openai:model@https://example.test/v1",
        llm_api_key_env="TEST_KEY",
        capabilities=ProfileCapabilities(
            structured_output_modes=declared,
            preferred_structured_output_mode=declared[0],
        ),
        recovery=ProfileRecovery(
            transient_retry_attempts=1,
            base_delay_seconds=0,
            max_delay_seconds=0,
        ),
    )
    return ModelExecutionGateway(adapter, profile, sleep=lambda _: None)


def test_invalid_edge_is_quarantined_and_valid_edge_survives(tmp_path):
    model = SimpleNamespace(
        invoke=lambda _: AIMessage(
            content=json.dumps(
                {
                    "items": [
                        {"source": "A", "target": "B"},
                        {"source": "B"},
                    ]
                }
            )
        )
    )
    store = ModelArtifactStore(tmp_path)
    invoker = StructuredOutputInvoker(
        model,
        EdgeList,
        mode="text_json",
        repair_attempts=0,
        invalid_item_ratio_threshold=0.6,
        artifact_store=store,
        request_metadata={"request_id": "edge-request", "chunk_id": "c1"},
    )
    result = invoker.invoke("extract")
    assert result.items == [Edge(source="A", target="B")]
    assert invoker.last_validation_summary.status == "completed_with_rejections"
    assert invoker.last_validation_summary.graph_connectivity_incomplete is True
    rejection_path = tmp_path / "rejections" / "edge-request.jsonl"
    rejection = json.loads(rejection_path.read_text(encoding="utf-8"))
    assert rejection["schema_path"] == "items.1.target"
    assert rejection["action"] == "quarantined"
    assert not any("bridge" in edge.source.lower() for edge in result.items)


def test_repair_repeats_up_to_budget_and_includes_invalid_json():
    responses = iter(
        [
            AIMessage(content='{"items":[{"source":"A"}]}'),
            AIMessage(content='{"items":[{"source":"A"}]}'),
            AIMessage(content='{"items":[{"source":"A","target":"B"}]}'),
        ]
    )
    prompts = []

    def invoke(prompt):
        prompts.append(prompt)
        return next(responses)

    result = StructuredOutputInvoker(
        SimpleNamespace(invoke=invoke),
        EdgeList,
        mode="text_json",
        repair_attempts=2,
        invalid_item_ratio_threshold=0,
    ).invoke("extract")
    assert result.items[0].target == "B"
    assert len(prompts) == 3
    assert '"source":"A"' in str(prompts[1])


def test_validation_retry_uses_independent_fresh_request_budget(tmp_path):
    responses = iter(
        [
            AIMessage(content='{"items":[{"source":"A"}]}'),
            AIMessage(content='{"items":[{"source":"A","target":"B"}]}'),
        ]
    )
    calls = []

    def invoke(prompt):
        calls.append(prompt)
        return next(responses)

    result = StructuredOutputInvoker(
        SimpleNamespace(invoke=invoke),
        EdgeList,
        mode="text_json",
        repair_attempts=0,
        validation_retry_attempts=1,
        invalid_item_ratio_threshold=0,
        artifact_store=ModelArtifactStore(tmp_path),
        request_metadata={"request_id": "validation-retry"},
    ).invoke("extract")

    assert result == EdgeList(items=[Edge(source="A", target="B")])
    assert len(calls) == 2
    rejection = json.loads(
        (tmp_path / "rejections" / "validation-retry.jsonl").read_text()
    )
    assert rejection["action"] == "repaired"
    assert rejection["validation_attempt"] == 0
    assert rejection["repair_attempt"] == 0


def test_successful_repair_writes_one_final_repaired_item(tmp_path):
    responses = iter(
        [
            AIMessage(content='{"items":[{"source":"A"}]}'),
            AIMessage(content='{"items":[{"source":"A","target":"B"}]}'),
        ]
    )
    store = ModelArtifactStore(tmp_path)
    invoker = StructuredOutputInvoker(
        SimpleNamespace(invoke=lambda _: next(responses)),
        EdgeList,
        mode="text_json",
        repair_attempts=1,
        invalid_item_ratio_threshold=0,
        artifact_store=store,
        request_metadata={"request_id": "repair-success"},
    )

    result = invoker.invoke("extract")

    assert result.items == [Edge(source="A", target="B")]
    lines = (tmp_path / "rejections" / "repair-success.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["action"] == "repaired"


def test_exhausted_repair_replaces_attempts_with_one_failed_item(tmp_path):
    responses = iter(
        [
            AIMessage(content='{"items":[{"source":"A"}]}'),
            AIMessage(content='{"items":[{"source":"A"}]}'),
        ]
    )
    store = ModelArtifactStore(tmp_path)
    invoker = StructuredOutputInvoker(
        SimpleNamespace(invoke=lambda _: next(responses)),
        EdgeList,
        mode="text_json",
        repair_attempts=1,
        invalid_item_ratio_threshold=0,
        artifact_store=store,
        request_metadata={"request_id": "repair-failed"},
    )

    with pytest.raises(OutputValidationError):
        invoker.invoke("extract")

    lines = (tmp_path / "rejections" / "repair-failed.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["action"] == "failed"


def test_repair_preserves_distinct_rejections_at_same_schema_path(tmp_path):
    responses = iter(
        [
            AIMessage(content='{"items":[{"source":"A"}]}'),
            AIMessage(content='{"items":[{"source":"B"}]}'),
        ]
    )
    invoker = StructuredOutputInvoker(
        SimpleNamespace(invoke=lambda _: next(responses)),
        EdgeList,
        mode="text_json",
        repair_attempts=1,
        invalid_item_ratio_threshold=0,
        artifact_store=ModelArtifactStore(tmp_path),
        request_metadata={"request_id": "repair-lineage"},
    )

    with pytest.raises(OutputValidationError):
        invoker.invoke("extract")

    lines = (tmp_path / "rejections" / "repair-lineage.jsonl").read_text().splitlines()
    items = [json.loads(line) for line in lines]
    assert len(items) == 2
    assert len({item["rejection_id"] for item in items}) == 2
    assert {item["repair_attempt"] for item in items} == {0, 1}
    assert {item["raw_item"]["source"] for item in items} == {"A", "B"}


def test_gateway_auto_uses_declared_profile_mode():
    adapter = SequenceAdapter(
        [GenerationResponse(request_id="r", final_text='{"items":[]}')]
    )
    invoker = StructuredOutputInvoker(
        SimpleNamespace(),
        EdgeList,
        mode="auto",
        gateway=_gateway(adapter),
    )

    assert invoker.invoke("extract") == EdgeList(items=[])
    assert [request.structured_output_mode for request in adapter.requests] == [
        "text_json"
    ]


def test_gateway_repair_reuses_profile_declared_tool_mode():
    adapter = SequenceAdapter(
        [
            GenerationResponse(request_id="r", final_text='{"items":[{"source":"A"}]}'),
            GenerationResponse(
                request_id="r",
                final_text='{"items":[{"source":"A","target":"B"}]}',
            ),
        ]
    )
    invoker = StructuredOutputInvoker(
        SimpleNamespace(),
        EdgeList,
        mode="auto",
        repair_attempts=1,
        invalid_item_ratio_threshold=0,
        gateway=_gateway(adapter, modes=["tool"]),
    )

    assert invoker.invoke("extract").items[0].target == "B"
    assert [request.structured_output_mode for request in adapter.requests] == [
        "tool",
        "tool",
    ]


def test_repair_prompt_redacts_provider_secrets():
    responses = iter(
        [
            AIMessage(content='{"items":[{"source":"sk-1234567890"}]}'),
            AIMessage(content='{"items":[{"source":"A","target":"B"}]}'),
        ]
    )
    prompts = []

    def invoke(prompt):
        prompts.append(prompt)
        return next(responses)

    StructuredOutputInvoker(
        SimpleNamespace(invoke=invoke),
        EdgeList,
        mode="text_json",
        repair_attempts=1,
        invalid_item_ratio_threshold=0,
    ).invoke("extract")

    assert "sk-1234567890" not in str(prompts[1])
    assert "[REDACTED]" in str(prompts[1])


def test_usage_counts_every_gateway_physical_attempt_and_actual_mode():
    failure = CanonicalModelFailure(
        request_id="r",
        category="transient",
        reason="server_error",
    )
    adapter = SequenceAdapter(
        [
            GenerationAdapterError("temporary", failure=failure),
            GenerationResponse(request_id="r", final_text='{"items":[]}'),
        ]
    )
    tracker = ModelUsageTracker()
    invoker = StructuredOutputInvoker(
        SimpleNamespace(),
        EdgeList,
        mode="auto",
        gateway=_gateway(adapter),
        usage_tracker=tracker,
    )

    invoker.invoke("extract")

    usage = tracker.snapshot()
    assert usage["total_calls"] == 2
    assert usage["failed_calls"] == 1
    assert usage["successful_calls"] == 1
    assert usage["by_mode"]["text_json"]["calls"] == 2
    assert usage["total_recovery_actions"] == 1
    assert usage["by_recovery_action"] == {"retry": 1}
