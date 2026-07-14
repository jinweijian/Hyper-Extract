import pytest

from hyperextract.methods.rag.course_knowledge_graph import CourseNode


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("skill", "method"),
        ("technique", "method"),
        ("framework", "model"),
        ("procedure", "process"),
        ("guideline", "rule"),
        ("function", "concept"),
        ("role", "concept"),
        ("capability", "concept"),
        ("概念", "concept"),
    ],
)
def test_course_node_normalizes_known_knowledge_kind_aliases(raw, expected):
    node = CourseNode(
        name="Alpha",
        level="point",
        summary="Definition.",
        evidence="Evidence.",
        knowledge_kind=raw,
    )
    assert node.knowledge_kind == expected


def test_course_node_rejects_unknown_knowledge_kind():
    with pytest.raises(ValueError, match="knowledge_kind"):
        CourseNode(
            name="Alpha",
            level="point",
            summary="Definition.",
            evidence="Evidence.",
            knowledge_kind="random-value",
        )
