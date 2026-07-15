"""Course-oriented GraphRAG method for assessable knowledge points."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from ontomem.merger import MergeStrategy
from pydantic import BaseModel, Field, field_validator

from hyperextract.briefs import ExtractionBrief, render_extraction_brief
from hyperextract.types import AutoGraph
from hyperextract.documents.structured_output import StructuredOutputInvoker
from hyperextract.documents.model_usage import ModelUsageTracker
from hyperextract.providers.artifacts import ModelArtifactStore
from hyperextract.providers.gateway import ModelExecutionGateway
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

_STAGE_MARKERS = {
    "nodes": "### 文档上下文",
    "chunk": "### 文档上下文",
    "local_edges": "### 给定知识点",
    "global_edges": "### 候选知识点对",
    "dedup": "A: {left}",
    "community": "知识点：",
}
_BRIEF_STAGES = {
    "nodes": "node_extraction",
    "chunk": "combined_local_extraction",
    "local_edges": "local_relation_extraction",
    "global_edges": "global_relation_extraction",
    "dedup": "deduplication",
    "community": "community",
}


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
        validation_retry_attempts: int | None = None,
        invalid_item_policy: Literal["quarantine", "fail"] = "quarantine",
        invalid_item_ratio_threshold: float = 0.2,
        extraction_brief: ExtractionBrief | None = None,
        generation_gateway: ModelExecutionGateway | None = None,
    ) -> None:
        self.profile = profile or _DEFAULT_PROFILE
        self._model_request_local = threading.local()
        self.extraction_brief = extraction_brief
        self.compiled_profile = compile_course_profile(self.profile)
        self.profile_name = self.profile.name
        self.profile_version = self.profile.version
        self.profile_hash = self.profile.content_hash
        self.brief_hash = extraction_brief.content_hash if extraction_brief else ""
        self.prompt_hash = self.compiled_profile.prompt_hash
        self.usage_tracker = ModelUsageTracker()
        self.structured_output_mode = structured_output_mode
        self.output_repair_attempts = output_repair_attempts
        self.generation_gateway = generation_gateway or getattr(
            llm_client, "model_execution_gateway", None
        )
        self.validation_retry_attempts = (
            validation_retry_attempts
            if validation_retry_attempts is not None
            else (
                self.generation_gateway.profile.recovery.validation_retry_attempts
                if self.generation_gateway is not None
                else 0
            )
        )
        self.invalid_item_policy = invalid_item_policy
        self.invalid_item_ratio_threshold = invalid_item_ratio_threshold
        self.model_artifact_store: ModelArtifactStore | None = None
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
        prompts = self._compile_prompt_messages()
        self.prompt_snapshots = prompts
        self.prompt_hash = hashlib.sha256(
            json.dumps(
                prompts,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.node_extractor = (
            self._chat_prompt(prompts["nodes"])
            | StructuredOutputInvoker(
                self.llm_client,
                self.node_list_schema,
                mode=output_mode,
                repair_attempts=repair_attempts,
                validation_retry_attempts=self.validation_retry_attempts,
                invalid_item_policy=self.invalid_item_policy,
                invalid_item_ratio_threshold=self.invalid_item_ratio_threshold,
                usage_tracker=self.usage_tracker,
                operation="local_nodes",
                artifact_store=self.model_artifact_store,
                gateway=self.generation_gateway,
                request_metadata=self._model_request_metadata,
            ).as_runnable()
        )
        self.chunk_extractor = (
            self._chat_prompt(prompts["chunk"])
            | StructuredOutputInvoker(
                self.llm_client,
                CourseChunkResult,
                mode=output_mode,
                repair_attempts=repair_attempts,
                validation_retry_attempts=self.validation_retry_attempts,
                invalid_item_policy=self.invalid_item_policy,
                invalid_item_ratio_threshold=self.invalid_item_ratio_threshold,
                usage_tracker=self.usage_tracker,
                operation="local_chunk",
                artifact_store=self.model_artifact_store,
                gateway=self.generation_gateway,
                request_metadata=self._model_request_metadata,
            ).as_runnable()
        )
        self.edge_extractor = (
            self._chat_prompt(prompts["local_edges"])
            | StructuredOutputInvoker(
                self.llm_client,
                self.edge_list_schema,
                mode=output_mode,
                repair_attempts=repair_attempts,
                validation_retry_attempts=self.validation_retry_attempts,
                invalid_item_policy=self.invalid_item_policy,
                invalid_item_ratio_threshold=self.invalid_item_ratio_threshold,
                usage_tracker=self.usage_tracker,
                operation="local_edges",
                artifact_store=self.model_artifact_store,
                gateway=self.generation_gateway,
                request_metadata=self._model_request_metadata,
            ).as_runnable()
        )
        self.global_edge_extractor = (
            self._chat_prompt(prompts["global_edges"])
            | StructuredOutputInvoker(
                self.llm_client,
                CourseEdgeList,
                mode=output_mode,
                repair_attempts=repair_attempts,
                validation_retry_attempts=self.validation_retry_attempts,
                invalid_item_policy=self.invalid_item_policy,
                invalid_item_ratio_threshold=self.invalid_item_ratio_threshold,
                usage_tracker=self.usage_tracker,
                operation="global_edges",
                artifact_store=self.model_artifact_store,
                gateway=self.generation_gateway,
                request_metadata=self._model_request_metadata,
            ).as_runnable()
        )
        self.dedup_extractor = (
            self._chat_prompt(prompts["dedup"])
            | StructuredOutputInvoker(
                self.llm_client,
                DedupDecision,
                mode=output_mode,
                repair_attempts=repair_attempts,
                validation_retry_attempts=self.validation_retry_attempts,
                invalid_item_policy=self.invalid_item_policy,
                invalid_item_ratio_threshold=self.invalid_item_ratio_threshold,
                usage_tracker=self.usage_tracker,
                operation="dedup",
                artifact_store=self.model_artifact_store,
                gateway=self.generation_gateway,
                request_metadata=self._model_request_metadata,
            ).as_runnable()
        )
        self.community_extractor = (
            self._chat_prompt(prompts["community"])
            | StructuredOutputInvoker(
                self.llm_client,
                CommunityReport,
                mode=output_mode,
                repair_attempts=repair_attempts,
                validation_retry_attempts=self.validation_retry_attempts,
                invalid_item_policy=self.invalid_item_policy,
                invalid_item_ratio_threshold=self.invalid_item_ratio_threshold,
                usage_tracker=self.usage_tracker,
                operation="community",
                artifact_store=self.model_artifact_store,
                gateway=self.generation_gateway,
                request_metadata=self._model_request_metadata,
            ).as_runnable()
        )

    def _model_request_metadata(self) -> dict[str, str]:
        profile_fingerprint = str(
            getattr(self, "model_profile_fingerprint", "")
            or (
                self.generation_gateway.profile.public_fingerprint()
                if self.generation_gateway is not None
                else ""
            )
        )
        model_fingerprint = str(getattr(self, "model_fingerprint", ""))
        if not model_fingerprint and self.generation_gateway is not None:
            model_fingerprint = hashlib.sha256(
                self.generation_gateway.profile.llm.encode("utf-8")
            ).hexdigest()
        metadata = {
            "profile_fingerprint": profile_fingerprint,
            "model_fingerprint": model_fingerprint,
            "prompt_fingerprint": self.prompt_hash,
        }
        metadata.update(getattr(self._model_request_local, "metadata", {}))
        return {key: value for key, value in metadata.items() if value}

    @contextmanager
    def model_request_context(self, **metadata: str | None) -> Iterator[None]:
        previous = getattr(self._model_request_local, "metadata", {})
        self._model_request_local.metadata = {
            **previous,
            **{key: str(value) for key, value in metadata.items() if value is not None},
        }
        try:
            yield
        finally:
            self._model_request_local.metadata = previous

    def _compile_prompt_messages(self) -> dict[str, dict[str, str]]:
        prompts = {
            "nodes": self.compiled_profile.nodes,
            "chunk": self.compiled_profile.chunk,
            "local_edges": self.compiled_profile.local_edges,
            "global_edges": self.compiled_profile.global_edges,
            "dedup": self.compiled_profile.dedup,
            "community": self.compiled_profile.community,
        }
        if self.extraction_brief is None:
            return {
                name: {"system": "", "user": prompt} for name, prompt in prompts.items()
            }
        compiled: dict[str, dict[str, str]] = {}
        for name, prompt in prompts.items():
            marker = _STAGE_MARKERS[name]
            if marker not in prompt:
                raise ValueError(f"Course profile prompt is missing marker: {marker}")
            profile_instructions, user = prompt.split(marker, maxsplit=1)
            user = f"{marker}{user}"
            compiled[name] = {
                "system": render_extraction_brief(
                    self.extraction_brief,
                    _BRIEF_STAGES[name],
                    profile_instructions=profile_instructions,
                ),
                "user": user,
            }
        return compiled

    @staticmethod
    def _chat_prompt(messages: dict[str, str]) -> ChatPromptTemplate:
        if not messages["system"]:
            return ChatPromptTemplate.from_template(messages["user"])
        system = messages["system"].replace("{", "{{").replace("}", "}}")
        return ChatPromptTemplate.from_messages(
            [("system", system), ("human", messages["user"])]
        )

    def apply_profile(self, profile: CourseExtractionProfile) -> None:
        """Replace all course-stage prompts with one validated profile."""
        self.profile = profile
        self.compiled_profile = compile_course_profile(profile)
        self.profile_name = profile.name
        self.profile_version = profile.version
        self.profile_hash = profile.content_hash
        self._configure_profile_extractors()

    def apply_extraction_brief(self, brief: ExtractionBrief | None) -> None:
        """Compile a package-owned run brief into every model stage."""
        self.extraction_brief = brief
        self.brief_hash = brief.content_hash if brief else ""
        self._configure_profile_extractors()

    def configure_model_artifacts(self, root: str | Path) -> None:
        """Route structured-output evidence into the current run checkpoint."""
        self.model_artifact_store = ModelArtifactStore(root)
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
            validation_retry_attempts=self.validation_retry_attempts,
            invalid_item_policy=self.invalid_item_policy,
            invalid_item_ratio_threshold=self.invalid_item_ratio_threshold,
            extraction_brief=self.extraction_brief,
            generation_gateway=self.generation_gateway,
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
