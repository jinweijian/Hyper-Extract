"""Versioned parser-neutral output contract for course knowledge graphs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import DocumentOutline, SourceReference


class _GraphModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


OutlineType = Literal["book", "part", "chapter", "section", "subsection"]
KnowledgeLevel = Literal["point", "sub_point"]
SemanticEdgeType = Literal["prerequisite", "derivative", "related", "confusable"]


class CourseOutlineNodeV1(_GraphModel):
    id: str
    name: str
    node_type: OutlineType
    depth: int = Field(ge=0)
    parent_id: str | None = None
    order: int = Field(ge=0)
    source_refs: list[SourceReference] = Field(default_factory=list)


class CourseKnowledgeNodeV1(_GraphModel):
    id: str
    name: str
    level: KnowledgeLevel
    parent_outline_id: str
    summary: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    source_refs: list[SourceReference] = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    knowledge_kind: str | None = None
    parent_knowledge_id: str | None = None
    aliases: list[str] = Field(default_factory=list)
    learning_objective: str | None = None
    confidence: float = Field(default=0.7, ge=0, le=1)


class CourseStructuralEdgeV1(_GraphModel):
    source_id: str
    target_id: str
    edge_type: Literal["contains", "describes"]
    system_generated: bool = True


class CourseSemanticEdgeV1(_GraphModel):
    source_id: str
    target_id: str
    edge_type: SemanticEdgeType
    evidence: str = Field(min_length=1)
    source_refs: list[SourceReference] = Field(min_length=1)
    confidence: float = Field(default=0.7, ge=0, le=1)
    status: Literal["pending", "approved", "rejected"] = "pending"


class CourseGraphV1(_GraphModel):
    schema_name: Literal["HyperExtractCourseGraph"] = "HyperExtractCourseGraph"
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    outline_nodes: list[CourseOutlineNodeV1]
    knowledge_nodes: list[CourseKnowledgeNodeV1]
    structural_edges: list[CourseStructuralEdgeV1]
    semantic_edges: list[CourseSemanticEdgeV1]

    @model_validator(mode="after")
    def validate_graph_references(self) -> CourseGraphV1:
        outline_ids = [node.id for node in self.outline_nodes]
        knowledge_ids = [node.id for node in self.knowledge_nodes]
        all_ids = outline_ids + knowledge_ids
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("Course Graph contains duplicate node ids")
        outline_set = set(outline_ids)
        knowledge_set = set(knowledge_ids)
        all_set = set(all_ids)

        for node in self.outline_nodes:
            if node.parent_id is not None and node.parent_id not in outline_set:
                raise ValueError(f"Course Graph outline has unknown parent: {node.id}")
        for node in self.knowledge_nodes:
            if node.parent_outline_id not in outline_set:
                raise ValueError(
                    f"Course Graph knowledge node has unknown outline: {node.id}"
                )
            if (
                node.parent_knowledge_id is not None
                and node.parent_knowledge_id not in knowledge_set
            ):
                raise ValueError(
                    f"Course Graph knowledge node has unknown parent knowledge: {node.id}"
                )
            if (
                node.profile_version != self.profile_version
                or node.run_id != self.run_id
            ):
                raise ValueError(
                    f"Course Graph knowledge node run fingerprint mismatch: {node.id}"
                )

        structural_keys: set[tuple[str, str, str]] = set()
        for edge in self.structural_edges:
            if not edge.system_generated:
                raise ValueError(
                    "Course Graph structural edges must be system-generated"
                )
            if edge.source_id not in all_set or edge.target_id not in all_set:
                raise ValueError("Course Graph structural edge has unknown endpoint")
            if edge.source_id == edge.target_id:
                raise ValueError("Course Graph structural edge self-loop")
            key = (edge.source_id, edge.target_id, edge.edge_type)
            if key in structural_keys:
                raise ValueError("Course Graph contains duplicate structural edge")
            structural_keys.add(key)

        semantic_keys: set[tuple[str, str, str]] = set()
        for edge in self.semantic_edges:
            if (
                edge.source_id not in knowledge_set
                or edge.target_id not in knowledge_set
            ):
                raise ValueError("Course Graph semantic edge has unknown endpoint")
            if edge.source_id == edge.target_id:
                raise ValueError("Course Graph semantic edge self-loop")
            source, target = edge.source_id, edge.target_id
            if edge.edge_type in {"related", "confusable"} and source > target:
                source, target = target, source
            key = (source, target, edge.edge_type)
            if key in semantic_keys:
                raise ValueError("Course Graph contains duplicate semantic edge")
            semantic_keys.add(key)
        return self


def _outline_type(level: int) -> OutlineType:
    if level == 0:
        return "book"
    if level == 1:
        return "chapter"
    if level == 2:
        return "section"
    return "subsection"


def build_course_graph_v1(
    outline: DocumentOutline,
    knowledge_nodes: list[CourseKnowledgeNodeV1],
    semantic_edges: list[CourseSemanticEdgeV1],
    *,
    run_id: str,
    profile_version: str,
) -> CourseGraphV1:
    """Build deterministic hierarchy and membership edges without model inference."""
    outline_nodes = [
        CourseOutlineNodeV1(
            id=node.id,
            name=node.title,
            node_type=_outline_type(node.level),
            depth=node.level,
            parent_id=node.parent_id,
            order=node.order,
            source_refs=node.source_refs,
        )
        for node in sorted(outline.nodes, key=lambda item: item.order)
    ]
    structural_edges = [
        CourseStructuralEdgeV1(
            source_id=node.parent_id,
            target_id=node.id,
            edge_type="contains",
        )
        for node in outline_nodes
        if node.parent_id is not None
    ]
    structural_edges.extend(
        CourseStructuralEdgeV1(
            source_id=node.parent_outline_id,
            target_id=node.id,
            edge_type="contains",
        )
        for node in knowledge_nodes
    )
    return CourseGraphV1(
        run_id=run_id,
        profile_version=profile_version,
        outline_nodes=outline_nodes,
        knowledge_nodes=knowledge_nodes,
        structural_edges=structural_edges,
        semantic_edges=semantic_edges,
    )
