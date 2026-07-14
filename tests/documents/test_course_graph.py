import pytest

from hyperextract.documents.course_graph import (
    CourseGraphV1,
    CourseKnowledgeNodeV1,
    CourseSemanticEdgeV1,
    build_course_graph_v1,
)
from hyperextract.documents.models import DocumentOutline, OutlineNode, SourceReference


def _outline():
    return DocumentOutline(
        document_name="Course",
        schema_name="HyperExtractOutline",
        schema_version="1.0",
        nodes=[
            OutlineNode(id="root", title="Course", level=0, order=0),
            OutlineNode(
                id="chapter-2", title="Chapter 2", level=1, parent_id="root", order=1
            ),
            OutlineNode(
                id="section-2-1",
                title="2.1 Topic",
                level=2,
                parent_id="chapter-2",
                order=2,
            ),
        ],
    )


def _knowledge():
    return CourseKnowledgeNodeV1(
        id="kp-alpha",
        name="Alpha",
        level="point",
        parent_outline_id="section-2-1",
        summary="Alpha is a defined course concept.",
        evidence="Definition alpha.",
        source_refs=[SourceReference(ref="source.md#L10-L10")],
        profile_version="course-v1",
        run_id="run-1",
    )


def test_build_course_graph_preserves_outline_and_derives_contains_edges():
    graph = build_course_graph_v1(
        _outline(),
        [_knowledge()],
        [],
        run_id="run-1",
        profile_version="course-v1",
    )

    assert graph.schema_name == "HyperExtractCourseGraph"
    assert graph.schema_version == "1.0"
    assert [node.name for node in graph.outline_nodes] == [
        "Course",
        "Chapter 2",
        "2.1 Topic",
    ]
    assert [
        (edge.source_id, edge.target_id, edge.edge_type)
        for edge in graph.structural_edges
    ] == [
        ("root", "chapter-2", "contains"),
        ("chapter-2", "section-2-1", "contains"),
        ("section-2-1", "kp-alpha", "contains"),
    ]
    assert all(edge.system_generated for edge in graph.structural_edges)


def test_course_graph_rejects_missing_sources_and_unknown_endpoints():
    with pytest.raises(ValueError, match="source_refs"):
        CourseKnowledgeNodeV1.model_validate(
            _knowledge().model_dump() | {"source_refs": []}
        )

    value = build_course_graph_v1(
        _outline(),
        [_knowledge()],
        [],
        run_id="run-1",
        profile_version="course-v1",
    ).model_dump()
    value["semantic_edges"] = [
        CourseSemanticEdgeV1(
            source_id="kp-alpha",
            target_id="kp-missing",
            edge_type="related",
            evidence="Explicitly compared in source.",
            source_refs=[SourceReference(ref="source.md#L10-L10")],
        ).model_dump()
    ]
    with pytest.raises(ValueError, match="unknown endpoint"):
        CourseGraphV1.model_validate(value)


def test_course_graph_rejects_model_generated_contains_and_self_loops():
    value = build_course_graph_v1(
        _outline(),
        [_knowledge()],
        [],
        run_id="run-1",
        profile_version="course-v1",
    ).model_dump()
    value["structural_edges"][0]["system_generated"] = False
    with pytest.raises(ValueError, match="system-generated"):
        CourseGraphV1.model_validate(value)

    value = build_course_graph_v1(
        _outline(),
        [_knowledge()],
        [],
        run_id="run-1",
        profile_version="course-v1",
    ).model_dump()
    value["semantic_edges"] = [
        {
            "source_id": "kp-alpha",
            "target_id": "kp-alpha",
            "edge_type": "related",
            "evidence": "bad",
            "source_refs": [{"ref": "source.md#L10-L10"}],
        }
    ]
    with pytest.raises(ValueError, match="self-loop"):
        CourseGraphV1.model_validate(value)
