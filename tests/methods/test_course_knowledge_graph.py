from hyperextract.methods.registry import get_method
from hyperextract.methods.rag.course_knowledge_graph import (
    CourseKnowledgeGraph,
    normalize_name,
    stable_node_id,
)
from hyperextract.profiles.course import load_course_profile
from tests.mocks import MockChatModel, MockEmbeddings


def test_course_method_is_registered_with_chinese_prompts():
    method = get_method("course_knowledge_graph")

    assert method is not None
    assert method["type"] == "graph"
    assert method["language"] == "zh"


def test_course_node_ids_are_stable_across_spacing_and_case():
    assert normalize_name("Project  Scope") == normalize_name("project-scope")
    assert stable_node_id("Project  Scope") == stable_node_id("project-scope")


def test_course_method_uses_compiled_profile_for_every_prompt_stage():
    profile = load_course_profile()
    graph = CourseKnowledgeGraph(
        llm_client=MockChatModel(),
        embedder=MockEmbeddings(dim=8),
        profile=profile,
    )

    assert graph.profile_name == profile.name
    assert graph.profile_version == profile.version
    assert graph.profile_hash == profile.content_hash
    assert "可独立" in graph.compiled_profile.nodes
    assert "同一次输出" in graph.compiled_profile.chunk
    assert "仅主题相近" in graph.compiled_profile.local_edges
    assert "仅主题相近" in graph.compiled_profile.global_edges
    assert "完全相同" in graph.compiled_profile.dedup
