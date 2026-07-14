import json
import threading
from types import SimpleNamespace

import pytest

from hyperextract.documents.checkpoint import RunCheckpoint
from hyperextract.documents.course_pipeline import (
    PipelineOptions,
    _apply_profile_quality_gates,
    _extract_chunk,
    _extract_local_edge_batch,
    _global_edge_candidates,
    _normalize_chunk_graph,
    _quality_report,
    run_course_document,
)
from hyperextract.documents.model_errors import OutputTruncatedError
from hyperextract.documents.model_errors import TransientModelError
from hyperextract.documents.models import DocumentChunk, DocumentOutline, OutlineNode
from hyperextract.methods.rag.course_knowledge_graph import (
    CourseChunkResult,
    CourseEdge,
    CourseEdgeList,
    CourseNode,
)
from hyperextract.profiles.course import load_course_profile
from tests.documents.test_document_package import _write_package
from tests.documents.test_docling import _document
from tests.mocks import MockEmbeddings


class _EmptyGlobalEdges:
    def invoke(self, _input):
        return CourseEdgeList(items=[])


class FakeCourseGraph:
    def __init__(self):
        self.embedder = MockEmbeddings(dim=8)
        self.global_edge_extractor = _EmptyGlobalEdges()
        self.metadata = {}
        self.community_hierarchy = {}
        self.community_reports = {}
        self.extract_calls = 0
        self._lock = threading.Lock()
        self._graph = SimpleNamespace(nodes=[], edges=[])

    def extract_nodes(self, source_text):
        with self._lock:
            self.extract_calls += 1
            index = self.extract_calls
        return [
            CourseNode(
                name=f"Knowledge {index}",
                level="point",
                summary=f"Definition {index}",
                evidence=f"Evidence {index}",
            )
        ]

    def extract_edges(self, source_text, nodes):
        return []

    def graph_schema(self, *, nodes, edges):
        return SimpleNamespace(nodes=nodes, edges=edges)

    def _set_data_state(self, graph):
        self._graph = graph

    def build_index(self):
        return None

    def dump(self, folder_path):
        folder_path.mkdir(parents=True, exist_ok=True)
        (folder_path / "data.json").write_text(
            json.dumps(
                {
                    "nodes": [node.model_dump() for node in self._graph.nodes],
                    "edges": [edge.model_dump() for edge in self._graph.edges],
                }
            ),
            encoding="utf-8",
        )
        (folder_path / "metadata.json").write_text(
            json.dumps(self.metadata), encoding="utf-8"
        )


class FakeCombinedCourseGraph(FakeCourseGraph):
    def extract_chunk_result(self, source_text):
        with self._lock:
            self.extract_calls += 1
        return CourseChunkResult(
            nodes=[
                CourseNode(
                    name="Combined knowledge",
                    level="point",
                    summary="Combined definition",
                    evidence="Combined evidence",
                )
            ],
            edges=[],
        )

    def extract_nodes(self, source_text):
        raise AssertionError("combined mode must not call extract_nodes")

    def extract_edges(self, source_text, nodes):
        raise AssertionError("combined mode must not call extract_edges")


def test_chunk_normalization_uses_exact_outline_title_for_parent():
    outline = DocumentOutline(
        document_name="Course",
        nodes=[
            OutlineNode(id="root", title="Course", level=0),
            OutlineNode(
                id="assets",
                title="2.2.2 组织过程资产",
                level=2,
                parent_id="root",
            ),
            OutlineNode(
                id="policies",
                title="2.2.3 政策、流程与程序",
                level=3,
                parent_id="assets",
            ),
        ],
    )
    chunk = DocumentChunk(
        id="chunk-1",
        index=0,
        outline_id="assets",
        top_level_id="assets",
        outline_path=["组织过程资产"],
        covered_outline_ids=["assets", "policies"],
        text="政策、流程与程序。",
        token_count=8,
    )
    result = CourseChunkResult(
        nodes=[
            CourseNode(
                name="政策、流程与程序",
                level="point",
                parent_outline_id="assets",
                summary="组织过程资产的一类",
                evidence="政策、流程与程序",
            )
        ],
        edges=[],
    )

    normalized = _normalize_chunk_graph(result, chunk, outline)

    assert normalized["nodes"][0]["parent_outline_id"] == "policies"


def test_course_pipeline_writes_outputs_and_resumes_chunks(tmp_path):
    source = tmp_path / "course.json"
    source.write_text(json.dumps(_document()), encoding="utf-8")
    output = tmp_path / "output"
    graph = FakeCourseGraph()
    options = PipelineOptions(
        target_tokens=100,
        max_tokens=200,
        max_workers=2,
        retry_attempts=1,
        heartbeat_interval=1,
        semantic_dedup=False,
        community_reports=False,
    )

    first = run_course_document(source, output, graph, options=options)
    calls_after_first_run = graph.extract_calls
    second = run_course_document(source, output, graph, options=options)

    assert first["status"] == "completed"
    assert second["run_id"] == first["run_id"]
    assert graph.extract_calls == calls_after_first_run
    assert (output / "course-graph.json").exists()
    assert (output / "quality-report.json").exists()
    assert (output / ".he-run" / "events.jsonl").exists()
    course_graph = json.loads(
        (output / "course-graph.json").read_text(encoding="utf-8")
    )
    assert course_graph["schema_name"] == "HyperExtractCourseGraph"
    assert course_graph["schema_version"] == "1.0"
    assert all(node["source_refs"] for node in course_graph["knowledge_nodes"])
    assert all(edge["system_generated"] for edge in course_graph["structural_edges"])


def test_course_pipeline_accepts_document_package_without_docling(tmp_path):
    source = _write_package(tmp_path / "course.hepkg")
    output = tmp_path / "output"
    graph = FakeCourseGraph()
    options = PipelineOptions(
        target_tokens=100,
        max_tokens=200,
        max_workers=1,
        retry_attempts=1,
        heartbeat_interval=1,
        semantic_dedup=False,
        community_reports=False,
    )

    result = run_course_document(
        source,
        output,
        graph,
        options=options,
        input_format="document-package",
    )

    assert result["status"] == "completed"
    assert result["input_format"] == "document-package"
    assert result["outline_nodes"] == 2
    assert graph.extract_calls == 1


def test_course_pipeline_combines_nodes_and_local_edges_in_one_model_call(tmp_path):
    source = _write_package(tmp_path / "course.hepkg")
    output = tmp_path / "output"
    graph = FakeCombinedCourseGraph()
    options = PipelineOptions(
        target_tokens=100,
        max_tokens=200,
        max_workers=1,
        retry_attempts=1,
        heartbeat_interval=1,
        semantic_dedup=False,
        community_reports=False,
        combined_local_extraction=True,
    )

    result = run_course_document(
        source,
        output,
        graph,
        options=options,
        input_format="document-package",
    )

    assert result["status"] == "completed"
    assert graph.extract_calls == 1
    assert (
        output / ".he-run" / "chunks" / "chunk-00000" / "chunk-result.json"
    ).exists()


def test_course_pipeline_records_profile_identity_and_rejects_changed_profile(
    tmp_path,
):
    source = _write_package(tmp_path / "course.hepkg")
    output = tmp_path / "output"
    options = PipelineOptions(
        target_tokens=100,
        max_tokens=200,
        max_workers=1,
        retry_attempts=1,
        heartbeat_interval=1,
        semantic_dedup=False,
        community_reports=False,
    )
    first_graph = FakeCourseGraph()
    first_graph.profile_name = "profile-a"
    first_graph.profile_version = "1.2.3"
    first_graph.profile_hash = "a" * 64
    first_graph.prompt_hash = "b" * 64

    run_course_document(
        source,
        output,
        first_graph,
        options=options,
        input_format="document-package",
    )

    run = json.loads((output / ".he-run" / "run.json").read_text(encoding="utf-8"))
    assert run["config"]["profile_name"] == "profile-a"
    assert run["config"]["profile_version"] == "1.2.3"
    assert run["config"]["profile_hash"] == "a" * 64
    graph = json.loads((output / "course-graph.json").read_text(encoding="utf-8"))
    assert graph["profile_version"] == "1.2.3"

    changed_graph = FakeCourseGraph()
    changed_graph.profile_name = "profile-a"
    changed_graph.profile_version = "1.2.3"
    changed_graph.profile_hash = "c" * 64
    changed_graph.prompt_hash = "d" * 64
    with pytest.raises(ValueError, match="checkpoint does not match"):
        run_course_document(
            source,
            output,
            changed_graph,
            options=options,
            input_format="document-package",
        )


def test_course_pipeline_resumes_when_only_non_prompt_profile_hash_changes(tmp_path):
    source = _write_package(tmp_path / "course.hepkg")
    output = tmp_path / "output"
    options = PipelineOptions(
        target_tokens=100,
        max_tokens=200,
        max_workers=1,
        retry_attempts=1,
        heartbeat_interval=1,
        semantic_dedup=False,
        community_reports=False,
    )
    first_graph = FakeCourseGraph()
    first_graph.profile_name = "profile-a"
    first_graph.profile_version = "1.2.3"
    first_graph.profile_hash = "a" * 64
    first_graph.prompt_hash = "b" * 64
    run_course_document(
        source,
        output,
        first_graph,
        options=options,
        input_format="document-package",
    )

    compatible_graph = FakeCourseGraph()
    compatible_graph.profile_name = "profile-a"
    compatible_graph.profile_version = "1.2.3"
    compatible_graph.profile_hash = "c" * 64
    compatible_graph.prompt_hash = "b" * 64
    result = run_course_document(
        source,
        output,
        compatible_graph,
        options=options,
        input_format="document-package",
    )

    assert result["status"] == "completed"
    assert compatible_graph.extract_calls == 0
    run = json.loads((output / ".he-run" / "run.json").read_text())
    assert run["config"]["profile_hash"] == "c" * 64


def test_transient_timeout_retries_same_chunk_without_splitting(tmp_path):
    class TimeoutGraph:
        def __init__(self):
            self.calls = 0

        def extract_nodes(self, _context):
            self.calls += 1
            raise TimeoutError("request timed out")

    graph = TimeoutGraph()
    outline = DocumentOutline(
        document_name="Course",
        nodes=[
            OutlineNode(id="root", title="Course", level=0),
            OutlineNode(
                id="section", title="Section", level=1, parent_id="root", order=1
            ),
        ],
    )
    chunk = DocumentChunk(
        id="chunk-1",
        index=0,
        outline_id="section",
        top_level_id="section",
        outline_path=["Section"],
        text="First paragraph.\n\nSecond paragraph.",
        token_count=8,
    )
    checkpoint = RunCheckpoint(
        tmp_path / "output",
        source_fingerprint="source",
        config={"test": True},
    )
    options = PipelineOptions(retry_attempts=1, heartbeat_interval=1)

    with pytest.raises(TransientModelError):
        _extract_chunk(graph, outline, chunk, [], checkpoint, options)

    assert graph.calls == 1
    assert not checkpoint.chunk_dir("chunk-1-a").joinpath("nodes.json").exists()


def test_quality_coverage_treats_grouping_outline_as_covered_by_descendant():
    outline = DocumentOutline(
        document_name="Course",
        nodes=[
            OutlineNode(id="root", title="Course", level=0),
            OutlineNode(id="group", title="Group", level=1, parent_id="root", order=1),
            OutlineNode(id="leaf", title="Leaf", level=2, parent_id="group", order=2),
        ],
    )
    knowledge = CourseNode(
        name="Knowledge",
        level="point",
        parent_outline_id="leaf",
        summary="Definition",
        evidence="Evidence",
        appearances=["leaf"],
    )

    report = _quality_report(
        outline, [knowledge], [], expected_outline_ids={"group", "leaf"}
    )

    assert report["outline_coverage"] == 1
    assert report["directly_covered_sections"] == 1
    assert report["hierarchically_covered_sections"] == 2
    assert report["uncovered_section_ids"] == []


def test_profile_quality_gate_rejects_invalid_nodes_and_relation_conflicts():
    outline = DocumentOutline(
        document_name="Course",
        nodes=[
            OutlineNode(id="root", title="Course", level=0),
            OutlineNode(
                id="section", title="Section", level=1, parent_id="root", order=1
            ),
        ],
    )
    alpha = CourseNode(
        name="Alpha",
        level="point",
        parent_outline_id="section",
        summary="Alpha definition",
        evidence="Alpha evidence",
        source_refs=["source.md#L1-L2"],
    )
    beta = CourseNode(
        name="Beta",
        level="point",
        parent_outline_id="section",
        summary="Beta definition",
        evidence="Beta evidence",
        source_refs=["source.md#L3-L4"],
    )
    invalid = CourseNode(
        name="No evidence",
        level="point",
        parent_outline_id="missing",
        summary="Definition",
        evidence="",
    )
    edges = [
        CourseEdge(
            source="Alpha",
            target="Beta",
            edge_type="prerequisite",
            description="Alpha is necessary for Beta",
        ),
        CourseEdge(
            source="Alpha",
            target="Beta",
            edge_type="related",
            description="They appear together",
        ),
        CourseEdge(
            source="Alpha",
            target="Unknown",
            edge_type="related",
            description="Unknown endpoint",
        ),
    ]

    nodes, accepted_edges, rejections = _apply_profile_quality_gates(
        load_course_profile(), outline, [alpha, beta, invalid], edges
    )

    assert [node.name for node in nodes] == ["Alpha", "Beta"]
    assert [(edge.edge_type, edge.source, edge.target) for edge in accepted_edges] == [
        ("prerequisite", "Alpha", "Beta")
    ]
    assert {item["reason"] for item in rejections} >= {
        "missing_evidence",
        "unknown_endpoint",
        "weaker_relation_conflict",
    }


def test_profile_quality_gate_repairs_exact_outline_title_assignment():
    outline = DocumentOutline(
        document_name="Course",
        nodes=[
            OutlineNode(id="root", title="Course", level=0),
            OutlineNode(
                id="assets",
                title="2.2.2 组织过程资产",
                level=2,
                parent_id="root",
            ),
            OutlineNode(
                id="policies",
                title="2.2.3 政策、流程与程序",
                level=2,
                parent_id="root",
            ),
        ],
    )
    node = CourseNode(
        name="政策、流程与程序",
        level="point",
        parent_outline_id="assets",
        summary="组织过程资产的一类",
        evidence="政策、流程与程序",
    )

    nodes, _, _ = _apply_profile_quality_gates(
        load_course_profile(), outline, [node], []
    )

    assert nodes[0].parent_outline_id == "policies"
    assert "policies" in nodes[0].appearances


def test_global_edge_candidates_stay_within_top_level_and_strict_top_k():
    outline = DocumentOutline(
        document_name="Course",
        nodes=[
            OutlineNode(id="root", title="Course", level=0),
            OutlineNode(id="chapter-a", title="A", level=1, parent_id="root", order=1),
            OutlineNode(id="a-1", title="A1", level=2, parent_id="chapter-a", order=2),
            OutlineNode(id="a-2", title="A2", level=2, parent_id="chapter-a", order=3),
            OutlineNode(id="chapter-b", title="B", level=1, parent_id="root", order=4),
            OutlineNode(id="b-1", title="B1", level=2, parent_id="chapter-b", order=5),
        ],
    )
    nodes = [
        CourseNode(
            name="A1",
            level="point",
            parent_outline_id="a-1",
            summary="A1",
            evidence="A1",
        ),
        CourseNode(
            name="A2",
            level="point",
            parent_outline_id="a-2",
            summary="A2",
            evidence="A2",
        ),
        CourseNode(
            name="B1",
            level="point",
            parent_outline_id="b-1",
            summary="B1",
            evidence="B1",
        ),
    ]
    embeddings = [[1.0, 0.0], [0.99, 0.01], [1.0, 0.0]]

    candidates = _global_edge_candidates(
        outline,
        nodes,
        embeddings,
        top_k=1,
        similarity_threshold=0.8,
    )

    assert [(item.left.name, item.right.name) for item in candidates] == [("A1", "A2")]


def test_global_edge_candidates_review_unresolved_same_section_pairs():
    outline = DocumentOutline(
        document_name="Course",
        nodes=[
            OutlineNode(id="root", title="Course", level=0),
            OutlineNode(id="chapter", title="A", level=1, parent_id="root", order=1),
            OutlineNode(
                id="section", title="A1", level=2, parent_id="chapter", order=2
            ),
        ],
    )
    nodes = [
        CourseNode(
            name=name,
            level="point",
            parent_outline_id="section",
            summary=name,
            evidence=name,
        )
        for name in ("Dimension A", "Dimension B", "Dimension C")
    ]
    existing = [
        CourseEdge(
            source="Dimension A",
            target="Dimension C",
            edge_type="related",
            description="Already covered",
        )
    ]

    candidates = _global_edge_candidates(
        outline,
        nodes,
        [[1.0, 0.0], [0.99, 0.01], [0.98, 0.02]],
        existing_edges=existing,
        top_k=2,
        similarity_threshold=0.8,
    )

    pairs = {frozenset((item.left.name, item.right.name)) for item in candidates}
    assert frozenset(("Dimension A", "Dimension B")) in pairs
    assert frozenset(("Dimension A", "Dimension C")) not in pairs


def test_global_edge_candidates_prefer_shared_suffix_contrast_pair():
    outline = DocumentOutline(
        document_name="Course",
        nodes=[
            OutlineNode(id="root", title="Course", level=0),
            OutlineNode(id="chapter", title="A", level=1, parent_id="root", order=1),
            OutlineNode(
                id="section", title="A1", level=2, parent_id="chapter", order=2
            ),
        ],
    )
    nodes = [
        CourseNode(
            name=name,
            level="point",
            parent_outline_id="section",
            summary=name,
            evidence=name,
        )
        for name in ("项目成功评估", "项目成果成功", "项目管理流程成功")
    ]
    existing = [
        CourseEdge(
            source="项目成功评估",
            target=target,
            edge_type="derivative",
            description="Dimension of evaluation",
        )
        for target in ("项目成果成功", "项目管理流程成功")
    ]

    candidates = _global_edge_candidates(
        outline,
        nodes,
        [[1.0, 0.0], [0.9, 0.1], [0.89, 0.11]],
        existing_edges=existing,
        top_k=1,
        similarity_threshold=0.99,
    )

    assert any(
        {item.left.name, item.right.name} == {"项目成果成功", "项目管理流程成功"}
        and "same_section_contrast" in item.selection_reasons
        for item in candidates
    )


def test_pipeline_defaults_keep_one_bounded_global_candidate_per_node():
    options = PipelineOptions()

    assert options.combined_local_extraction is False
    assert options.global_edge_top_k == 1
    assert options.global_edge_similarity_threshold == 0.7


def test_truncated_local_edge_batch_is_split_and_checkpointed(tmp_path):
    class TruncatingGraph:
        def __init__(self):
            self.batch_sizes = []

        def extract_edges(self, _context, nodes):
            self.batch_sizes.append(len(nodes))
            if len(nodes) > 2:
                raise OutputTruncatedError("finish_reason=length")
            return [
                CourseEdge(
                    source=nodes[0].name,
                    target=nodes[-1].name,
                    edge_type="related",
                    description="Direct comparison",
                )
            ]

    nodes = [
        CourseNode(
            name=f"K{index}",
            level="point",
            summary=f"Definition {index}",
            evidence=f"Evidence {index}",
        )
        for index in range(4)
    ]
    checkpoint = RunCheckpoint(
        tmp_path / "output",
        source_fingerprint="source",
        config={"test": True},
    )
    graph = TruncatingGraph()
    batch_path = checkpoint.chunk_dir("chunk-1") / "local-edge-batches/batch.json"

    edges = _extract_local_edge_batch(
        graph,
        "context",
        nodes,
        batch_path,
        checkpoint,
        PipelineOptions(retry_attempts=1, heartbeat_interval=1),
        chunk_id="chunk-1",
        label="关系抽取 chunk-1 批次 1/1",
    )

    assert graph.batch_sizes == [4, 2, 2]
    assert len(edges) == 2
    assert batch_path.exists()
    assert batch_path.with_name("batch-a.json").exists()
    assert batch_path.with_name("batch-b.json").exists()
