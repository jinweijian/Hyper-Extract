import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from pydantic import BaseModel

from hyperextract.documents.structured_output import StructuredOutputInvoker
from hyperextract.providers.artifacts import ModelArtifactStore


class Edge(BaseModel):
    source: str
    target: str


class EdgeList(BaseModel):
    items: list[Edge]


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
