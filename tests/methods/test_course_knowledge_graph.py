from hyperextract.methods.registry import get_method
from hyperextract.methods.rag.course_knowledge_graph import (
    CourseKnowledgeGraph,
    normalize_name,
    stable_node_id,
)
from hyperextract.profiles.course import load_course_profile
from hyperextract.briefs import ExtractionBrief
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


def test_course_method_compiles_brief_into_stage_system_messages():
    brief = ExtractionBrief.model_validate(
        {
            "schema_name": "HyperExtractExtractionBrief",
            "schema_version": "1.0",
            "metadata": {"id": "pmbok", "version": "1"},
            "task": {"objective": "Build a navigable knowledge graph"},
            "stage_instructions": {
                "node_extraction": ["Keep section hierarchy"],
                "global_relation_extraction": ["Prefer explicit dependencies"],
            },
        }
    )
    graph = CourseKnowledgeGraph(
        llm_client=MockChatModel(),
        embedder=MockEmbeddings(dim=8),
        extraction_brief=brief,
    )

    assert "Keep section hierarchy" in graph.prompt_snapshots["nodes"]["system"]
    assert (
        "Prefer explicit dependencies" not in graph.prompt_snapshots["nodes"]["system"]
    )
    assert (
        "Prefer explicit dependencies"
        in graph.prompt_snapshots["global_edges"]["system"]
    )
    assert "{source_text}" in graph.prompt_snapshots["nodes"]["user"]
    assert "{source_text}" not in graph.prompt_snapshots["nodes"]["system"]
    assert graph.brief_hash == brief.content_hash


def test_course_invokers_receive_thread_local_request_lineage():
    graph = CourseKnowledgeGraph(
        llm_client=MockChatModel(),
        embedder=MockEmbeddings(dim=8),
    )
    graph.model_profile_fingerprint = "profile-fingerprint"
    graph.model_fingerprint = "model-fingerprint"
    invoker = graph.chunk_extractor.steps[-1].func.__self__

    with graph.model_request_context(chunk_id="chunk-1", batch_id="chunk-result"):
        metadata = invoker.request_metadata

    assert metadata["chunk_id"] == "chunk-1"
    assert metadata["batch_id"] == "chunk-result"
    assert metadata["profile_fingerprint"] == "profile-fingerprint"
    assert metadata["model_fingerprint"] == "model-fingerprint"
    assert metadata["prompt_fingerprint"] == graph.prompt_hash
