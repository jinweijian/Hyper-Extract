"""Course-oriented GraphRAG method for assessable knowledge points."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from ontomem.merger import MergeStrategy
from pydantic import BaseModel, Field, field_validator

from hyperextract.types import AutoGraph
from hyperextract.documents.structured_output import StructuredOutputInvoker
from hyperextract.documents.model_usage import ModelUsageTracker
from hyperextract.profiles.course import (
    CourseExtractionProfile,
    compile_course_profile,
    load_course_profile,
)


KnowledgeLevel = Literal["point", "sub_point"]
EdgeType = Literal["prerequisite", "related", "derivative", "confusable"]
KnowledgeKind = Literal[
    "concept", "principle", "method", "process", "tool", "model", "rule"
]
_DEFAULT_PROFILE = load_course_profile()
_DEFAULT_COMPILED_PROFILE = compile_course_profile(_DEFAULT_PROFILE)
COURSE_PROFILE_VERSION = _DEFAULT_PROFILE.version


class CourseNode(BaseModel):
    id: str = Field(
        default="", description="Stable node identifier; leave empty during extraction"
    )
    name: str = Field(description="Short, stable course knowledge point name")
    level: KnowledgeLevel = Field(description="point or sub_point")
    parent_outline_id: str = Field(
        default="", description="Outline section ID from the input context"
    )
    summary: str = Field(
        description="One or two sentence independently understandable definition"
    )
    evidence: str = Field(
        description="Short source quote that directly supports this knowledge point"
    )
    knowledge_kind: KnowledgeKind | None = None
    parent_knowledge_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    learning_objective: str | None = None
    confidence: float = Field(default=0.7, ge=0, le=1)
    source_refs: list[str] = Field(default_factory=list)
    appearances: list[str] = Field(default_factory=list)

    @field_validator("knowledge_kind", mode="before")
    @classmethod
    def normalize_knowledge_kind(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        aliases = {
            "skill": "method",
            "technique": "method",
            "framework": "model",
            "procedure": "process",
            "guideline": "rule",
            "function": "concept",
            "role": "concept",
            "capability": "concept",
            "概念": "concept",
            "原则": "principle",
            "方法": "method",
            "流程": "process",
            "工具": "tool",
            "模型": "model",
            "规则": "rule",
        }
        return aliases.get(normalized, normalized)


class CourseEdge(BaseModel):
    source: str = Field(description="Exact source knowledge point name")
    target: str = Field(description="Exact target knowledge point name")
    edge_type: EdgeType
    weight: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.7, ge=0, le=1)
    description: str = Field(description="Concise evidence-based explanation")
    status: Literal["pending", "approved", "rejected"] = "pending"
    source_refs: list[str] = Field(default_factory=list)


class CourseEdgeList(BaseModel):
    items: list[CourseEdge] = Field(default_factory=list)


class CourseChunkResult(BaseModel):
    nodes: list[CourseNode] = Field(default_factory=list)
    edges: list[CourseEdge] = Field(default_factory=list)


class DedupDecision(BaseModel):
    same: bool
    preferred_name: str = ""
    reason: str = ""


class CommunityReport(BaseModel):
    id: str = ""
    title: str
    summary: str
    key_entities: list[str] = Field(default_factory=list)


NODE_PROMPT = _DEFAULT_COMPILED_PROFILE.nodes
EDGE_PROMPT = _DEFAULT_COMPILED_PROFILE.local_edges
GLOBAL_EDGE_PROMPT = _DEFAULT_COMPILED_PROFILE.global_edges
DEDUP_PROMPT = _DEFAULT_COMPILED_PROFILE.dedup
COMMUNITY_PROMPT = _DEFAULT_COMPILED_PROFILE.community


def normalize_name(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value.lower())


def stable_node_id(name: str) -> str:
    return "kp-" + hashlib.sha1(normalize_name(name).encode("utf-8")).hexdigest()[:16]


class CourseKnowledgeGraph(AutoGraph[CourseNode, CourseEdge]):
    """GraphRAG preset used by the structured book pipeline."""

    def __init__(
        self,
        llm_client: BaseChatModel,
        embedder: Embeddings,
        chunk_size: int = 1_000_000,
        chunk_overlap: int = 0,
        max_workers: int = 1,
        verbose: bool = False,
        profile: CourseExtractionProfile | None = None,
        structured_output_mode: str | None = None,
        output_repair_attempts: int | None = None,
    ) -> None:
        self.profile = profile or _DEFAULT_PROFILE
        self.compiled_profile = compile_course_profile(self.profile)
        self.profile_name = self.profile.name
        self.profile_version = self.profile.version
        self.profile_hash = self.profile.content_hash
        self.prompt_hash = self.compiled_profile.prompt_hash
        self.usage_tracker = ModelUsageTracker()
        self.structured_output_mode = structured_output_mode
        self.output_repair_attempts = output_repair_attempts
        super().__init__(
            node_schema=CourseNode,
            edge_schema=CourseEdge,
            node_key_extractor=lambda node: node.name,
            edge_key_extractor=lambda edge: (
                normalize_name(edge.source),
                normalize_name(edge.target),
                edge.edge_type,
            ),
            nodes_in_edge_extractor=lambda edge: (edge.source, edge.target),
            llm_client=llm_client,
            embedder=embedder,
            extraction_mode="two_stage",
            node_strategy_or_merger=MergeStrategy.KEEP_EXISTING,
            edge_strategy_or_merger=MergeStrategy.KEEP_EXISTING,
            prompt_for_node_extraction=self.compiled_profile.nodes,
            prompt_for_edge_extraction=self.compiled_profile.local_edges,
            node_label_extractor=lambda node: node.name,
            edge_label_extractor=lambda edge: edge.edge_type,
            node_fields_for_index=["name", "summary"],
            edge_fields_for_index=["description"],
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            max_workers=max_workers,
            verbose=verbose,
        )
        self._configure_profile_extractors()
        self.community_reports: dict[str, CommunityReport] = {}
        self.community_hierarchy: dict[str, list[str]] = {}

    def _configure_profile_extractors(self) -> None:
        output_mode = self.structured_output_mode or os.environ.get(
            "HYPER_EXTRACT_STRUCTURED_OUTPUT_MODE", "auto"
        )
        repair_attempts = (
            self.output_repair_attempts
            if self.output_repair_attempts is not None
            else int(os.environ.get("HYPER_EXTRACT_OUTPUT_REPAIR_ATTEMPTS", "1"))
        )
        self.node_extractor = (
            ChatPromptTemplate.from_template(self.compiled_profile.nodes)
            | StructuredOutputInvoker(
                self.llm_client,
                self.node_list_schema,
                mode=output_mode,
                repair_attempts=repair_attempts,
                usage_tracker=self.usage_tracker,
                operation="local_nodes",
            ).as_runnable()
        )
        self.chunk_extractor = (
            ChatPromptTemplate.from_template(self.compiled_profile.chunk)
            | StructuredOutputInvoker(
                self.llm_client,
                CourseChunkResult,
                mode=output_mode,
                repair_attempts=repair_attempts,
                usage_tracker=self.usage_tracker,
                operation="local_chunk",
            ).as_runnable()
        )
        self.edge_extractor = (
            ChatPromptTemplate.from_template(self.compiled_profile.local_edges)
            | StructuredOutputInvoker(
                self.llm_client,
                self.edge_list_schema,
                mode=output_mode,
                repair_attempts=repair_attempts,
                usage_tracker=self.usage_tracker,
                operation="local_edges",
            ).as_runnable()
        )
        self.global_edge_extractor = (
            ChatPromptTemplate.from_template(self.compiled_profile.global_edges)
            | StructuredOutputInvoker(
                self.llm_client,
                CourseEdgeList,
                mode=output_mode,
                repair_attempts=repair_attempts,
                usage_tracker=self.usage_tracker,
                operation="global_edges",
            ).as_runnable()
        )
        self.dedup_extractor = (
            ChatPromptTemplate.from_template(self.compiled_profile.dedup)
            | StructuredOutputInvoker(
                self.llm_client,
                DedupDecision,
                mode=output_mode,
                repair_attempts=repair_attempts,
                usage_tracker=self.usage_tracker,
                operation="dedup",
            ).as_runnable()
        )
        self.community_extractor = (
            ChatPromptTemplate.from_template(self.compiled_profile.community)
            | StructuredOutputInvoker(
                self.llm_client,
                CommunityReport,
                mode=output_mode,
                repair_attempts=repair_attempts,
                usage_tracker=self.usage_tracker,
                operation="community",
            ).as_runnable()
        )

    def apply_profile(self, profile: CourseExtractionProfile) -> None:
        """Replace all course-stage prompts with one validated profile."""
        self.profile = profile
        self.compiled_profile = compile_course_profile(profile)
        self.profile_name = profile.name
        self.profile_version = profile.version
        self.profile_hash = profile.content_hash
        self.prompt_hash = self.compiled_profile.prompt_hash
        self._configure_profile_extractors()

    def _create_empty_instance(self) -> "CourseKnowledgeGraph":
        return CourseKnowledgeGraph(
            llm_client=self.llm_client,
            embedder=self.embedder,
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            max_workers=self.max_workers,
            verbose=self.verbose,
            profile=self.profile,
            structured_output_mode=self.structured_output_mode,
            output_repair_attempts=self.output_repair_attempts,
        )

    def extract_nodes(self, source_text: str) -> list[CourseNode]:
        node_list = self.node_extractor.invoke({"source_text": source_text})
        if node_list is None:
            raise ValueError("Model returned no structured node result")
        return node_list.items

    def extract_chunk_result(self, source_text: str) -> CourseChunkResult:
        result = self.chunk_extractor.invoke({"source_text": source_text})
        if result is None:
            raise ValueError("Model returned no structured chunk result")
        return result

    def extract_edges(
        self, source_text: str, nodes: list[CourseNode]
    ) -> list[CourseEdge]:
        known = "\n- ".join(node.name for node in nodes) or "无"
        edge_list = self.edge_extractor.invoke(
            {"source_text": source_text, "known_nodes": known}
        )
        if edge_list is None:
            raise ValueError("Model returned no structured edge result")
        return edge_list.items

    def extract_chunk(self, source_text: str):
        nodes = self.extract_nodes(source_text)
        edges = self.extract_edges(source_text, nodes)
        return self.graph_schema(nodes=nodes, edges=edges)

    def dump(self, folder_path: str | Path) -> None:
        super().dump(folder_path)
        index_path = Path(folder_path) / "index"
        if index_path.exists() and not any(
            path.is_file() for path in index_path.rglob("*")
        ):
            for directory in sorted(
                (path for path in index_path.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                directory.rmdir()
            index_path.rmdir()
        if self.community_reports or self.community_hierarchy:
            path = Path(folder_path) / "community_data.json"
            with path.open("w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "reports": {
                            key: value.model_dump()
                            for key, value in self.community_reports.items()
                        },
                        "hierarchy": self.community_hierarchy,
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )

    def load(self, folder_path: str | Path) -> None:
        super().load(folder_path)
        path = Path(folder_path) / "community_data.json"
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.community_reports = {
                key: CommunityReport.model_validate(value)
                for key, value in (data.get("reports") or {}).items()
            }
            self.community_hierarchy = data.get("hierarchy") or {}

    def detect_communities(self) -> dict[str, list[str]]:
        names = [node.name for node in self.nodes]
        adjacency = {name: set() for name in names}
        for edge in self.edges:
            if edge.source in adjacency and edge.target in adjacency:
                adjacency[edge.source].add(edge.target)
                adjacency[edge.target].add(edge.source)

        communities: list[list[str]] = []
        try:
            import networkx as nx

            graph = nx.Graph()
            graph.add_nodes_from(names)
            graph.add_edges_from((edge.source, edge.target) for edge in self.edges)
            if graph.number_of_edges():
                communities = [
                    sorted(community)
                    for community in nx.community.louvain_communities(graph, seed=0)
                ]
            else:
                communities = [[str(node)] for node in graph.nodes]
        except ImportError:
            unseen = set(names)
            while unseen:
                seed = unseen.pop()
                component = {seed}
                queue = [seed]
                while queue:
                    current = queue.pop()
                    for neighbor in adjacency[current] & unseen:
                        unseen.remove(neighbor)
                        component.add(neighbor)
                        queue.append(neighbor)
                communities.append(sorted(component))

        self.community_hierarchy = {
            f"community-{index:04d}": community
            for index, community in enumerate(communities)
            if community
        }
        return self.community_hierarchy

    def summarize_community(
        self,
        community_id: str,
        community: list[str],
    ) -> CommunityReport | None:
        node_lines = [
            f"- {node.name}: {node.summary}"
            for node in self.nodes
            if node.name in community
        ]
        edge_lines = [
            f"- {edge.source} -> {edge.target} ({edge.edge_type}): {edge.description}"
            for edge in self.edges
            if edge.source in community and edge.target in community
        ]
        report = self.community_extractor.invoke(
            {"nodes": "\n".join(node_lines), "edges": "\n".join(edge_lines)}
        )
        if report is not None:
            report.id = community_id
            report.key_entities = community[:10]
        return report

    def build_communities(self, *, generate_reports: bool = True) -> None:
        self.detect_communities()
        if not generate_reports:
            return
        self.community_reports = {}
        for community_id, community in self.community_hierarchy.items():
            report = self.summarize_community(community_id, community)
            if report is not None:
                self.community_reports[community_id] = report
