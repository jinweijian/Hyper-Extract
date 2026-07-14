"""Deterministic Gold Dataset evaluation for Course Graph v1."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hyperextract.documents.course_graph import CourseGraphV1
from hyperextract.profiles.course import (
    EvaluationThresholds,
    normalize_profile_name,
)


class _EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


GoldLabel = Literal["required", "acceptable", "forbidden"]
RelationLabel = Literal["required", "acceptable", "forbidden"]
RelationType = Literal["prerequisite", "derivative", "related", "confusable"]


class GoldSource(_EvaluationModel):
    document_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_name: str | None = None
    description: str | None = None


class AnnotationDecision(_EvaluationModel):
    annotator: str = Field(min_length=1)
    label: GoldLabel
    note: str | None = None


class GoldNode(_EvaluationModel):
    id: str = Field(min_length=1)
    label: GoldLabel
    canonical_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    outline_id: str | None = None
    evidence: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    annotations: list[AnnotationDecision] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_outline(self) -> "GoldNode":
        if self.label != "forbidden" and not self.outline_id:
            raise ValueError("required/acceptable gold nodes need outline_id")
        annotators = [item.annotator for item in self.annotations]
        if len(annotators) != len(set(annotators)):
            raise ValueError(f"duplicate annotator for gold node {self.id}")
        return self

    @property
    def normalized_names(self) -> set[str]:
        return {
            normalize_profile_name(value)
            for value in [self.canonical_name, *self.aliases]
            if value.strip()
        }


class GoldRelation(_EvaluationModel):
    id: str = Field(min_length=1)
    label: RelationLabel
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    relation_type: RelationType
    evidence: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class CourseGoldDataset(_EvaluationModel):
    schema_name: Literal["HyperExtractCourseGoldDataset"]
    schema_version: Literal["1.0"]
    dataset_id: str = Field(min_length=1)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    status: Literal["draft", "reviewed"] = "draft"
    source: GoldSource
    scope_outline_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    thresholds: EvaluationThresholds = Field(default_factory=EvaluationThresholds)
    nodes: list[GoldNode] = Field(min_length=1)
    relations: list[GoldRelation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> "CourseGoldDataset":
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Gold Dataset contains duplicate node ids")
        relation_ids = [relation.id for relation in self.relations]
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("Gold Dataset contains duplicate relation ids")
        known = set(node_ids)
        for relation in self.relations:
            if relation.source not in known or relation.target not in known:
                raise ValueError(f"Gold relation has unknown endpoint: {relation.id}")
            if relation.source == relation.target:
                raise ValueError(f"Gold relation is a self-loop: {relation.id}")
            labels = {node.id: node.label for node in self.nodes if node.id in known}
            if (
                labels[relation.source] == "forbidden"
                or labels[relation.target] == "forbidden"
            ):
                raise ValueError(
                    f"Gold relation cannot reference forbidden node: {relation.id}"
                )
        return self


class CourseEvaluationMetrics(_EvaluationModel):
    required_recall: float
    effective_precision: float
    forbidden_leakage_rate: float
    outline_accuracy: float
    extractable_outline_coverage: float
    evidence_coverage: float
    duplicate_rate: float
    relation_precision: float
    relation_recall: float
    annotator_agreement: float
    annotation_pairs: int
    predicted_nodes: int
    matched_required: int
    required_total: int
    matched_acceptable: int
    forbidden_leaks: int
    predicted_relations: int
    matched_relations: int


class CourseEvaluationReport(_EvaluationModel):
    schema_name: Literal["HyperExtractCourseEvaluationReport"] = (
        "HyperExtractCourseEvaluationReport"
    )
    schema_version: Literal["1.0"] = "1.0"
    dataset_id: str
    dataset_version: str
    graph_run_id: str
    graph_profile_version: str
    metrics: CourseEvaluationMetrics
    thresholds: EvaluationThresholds
    gates: dict[str, bool]
    passed: bool
    missing_required: list[str]
    forbidden_leaks: list[str]
    unmatched_predictions: list[str]
    outline_mismatches: list[str]
    missing_required_relations: list[str]
    unmatched_relations: list[str]
    unscored_relations: list[str]


def load_gold_dataset(path: str | Path) -> CourseGoldDataset:
    source = Path(path)
    return CourseGoldDataset.model_validate_json(source.read_text(encoding="utf-8"))


def _ratio(numerator: int, denominator: int, *, empty: float = 1.0) -> float:
    return round(numerator / denominator, 6) if denominator else empty


def _gold_for_prediction(
    name: str, outline_id: str, nodes: list[GoldNode]
) -> GoldNode | None:
    normalized = normalize_profile_name(name)
    matches = [node for node in nodes if normalized in node.normalized_names]
    scoped_positive = [
        node
        for node in matches
        if node.label in {"required", "acceptable"} and node.outline_id == outline_id
    ]
    if len(scoped_positive) == 1:
        return scoped_positive[0]
    if len(scoped_positive) > 1:
        raise ValueError(f"Prediction matches multiple scoped Gold nodes: {name}")
    if len(matches) > 1:
        raise ValueError(f"Prediction matches multiple Gold nodes: {name}")
    return matches[0] if matches else None


def _relation_key(
    source: str,
    target: str,
    relation_type: str,
) -> tuple[str, str, str]:
    source_name = normalize_profile_name(source)
    target_name = normalize_profile_name(target)
    if relation_type in {"related", "confusable"} and source_name > target_name:
        source_name, target_name = target_name, source_name
    return source_name, target_name, relation_type


def _annotator_agreement(nodes: list[GoldNode]) -> tuple[float, int]:
    equal = 0
    compared = 0
    for node in nodes:
        decisions = node.annotations
        for left in range(len(decisions)):
            for right in range(left + 1, len(decisions)):
                compared += 1
                equal += int(decisions[left].label == decisions[right].label)
    return (_ratio(equal, compared, empty=0.0), compared)


def evaluate_course_profile(
    dataset_path: str | Path,
    graph_path: str | Path,
    *,
    thresholds: EvaluationThresholds | None = None,
) -> CourseEvaluationReport:
    """Evaluate a Course Graph without any model calls or embedding dependency."""
    dataset = load_gold_dataset(dataset_path)
    graph = CourseGraphV1.model_validate_json(
        Path(graph_path).read_text(encoding="utf-8")
    )
    thresholds = thresholds or dataset.thresholds

    gold_by_id = {node.id: node for node in dataset.nodes}
    predicted_matches: dict[str, GoldNode] = {}
    unmatched_predictions: list[str] = []
    forbidden_leaks: list[str] = []
    outline_mismatches: list[str] = []
    matched_positive_ids: set[str] = set()
    correct_outline_ids: set[str] = set()

    for predicted in graph.knowledge_nodes:
        match = _gold_for_prediction(
            predicted.name, predicted.parent_outline_id, dataset.nodes
        )
        if match is None:
            unmatched_predictions.append(predicted.name)
            continue
        predicted_matches[predicted.id] = match
        if match.label == "forbidden":
            forbidden_leaks.append(predicted.name)
            continue
        matched_positive_ids.add(match.id)
        if predicted.parent_outline_id == match.outline_id:
            correct_outline_ids.add(match.id)
        else:
            outline_mismatches.append(
                f"{predicted.name}: {predicted.parent_outline_id} != {match.outline_id}"
            )

    required = [node for node in dataset.nodes if node.label == "required"]
    acceptable = [node for node in dataset.nodes if node.label == "acceptable"]
    forbidden = [node for node in dataset.nodes if node.label == "forbidden"]
    matched_required = [node for node in required if node.id in matched_positive_ids]
    matched_acceptable = [
        node for node in acceptable if node.id in matched_positive_ids
    ]
    positive_prediction_count = sum(
        1
        for match in predicted_matches.values()
        if match.label in {"required", "acceptable"}
    )
    matched_positive_prediction_count = positive_prediction_count

    extractable_outlines = {
        node.outline_id for node in [*required, *acceptable] if node.outline_id
    }
    covered_outlines = {
        gold_by_id[gold_id].outline_id
        for gold_id in correct_outline_ids
        if gold_by_id[gold_id].outline_id
    }
    matched_positive_count = len(matched_required) + len(matched_acceptable)
    correct_outline_count = sum(
        1 for gold_id in matched_positive_ids if gold_id in correct_outline_ids
    )
    evidence_count = sum(
        bool(node.evidence.strip()) and bool(node.source_refs)
        for node in graph.knowledge_nodes
    )
    normalized_names = [
        normalize_profile_name(node.name) for node in graph.knowledge_nodes
    ]
    duplicate_count = len(normalized_names) - len(set(normalized_names))

    gold_relation_keys: dict[tuple[str, str, str], GoldRelation] = {}
    scoped_relation_pairs: set[frozenset[str]] = set()
    for relation in dataset.relations:
        source = gold_by_id[relation.source].canonical_name
        target = gold_by_id[relation.target].canonical_name
        key = _relation_key(source, target, relation.relation_type)
        gold_relation_keys[key] = relation
        scoped_relation_pairs.add(
            frozenset(
                {
                    normalize_profile_name(source),
                    normalize_profile_name(target),
                }
            )
        )

    matched_relation_ids: set[str] = set()
    unmatched_relations: list[str] = []
    unscored_relations: list[str] = []
    assessed_relation_count = 0
    for edge in graph.semantic_edges:
        source_match = predicted_matches.get(edge.source_id)
        target_match = predicted_matches.get(edge.target_id)
        if (
            source_match is None
            or target_match is None
            or source_match.label == "forbidden"
            or target_match.label == "forbidden"
        ):
            unmatched_relations.append(
                f"{edge.source_id} -> {edge.target_id} ({edge.edge_type})"
            )
            continue
        pair = frozenset(
            {
                normalize_profile_name(source_match.canonical_name),
                normalize_profile_name(target_match.canonical_name),
            }
        )
        if pair not in scoped_relation_pairs:
            unscored_relations.append(
                f"{source_match.canonical_name} -> {target_match.canonical_name} "
                f"({edge.edge_type})"
            )
            continue
        assessed_relation_count += 1
        key = _relation_key(
            source_match.canonical_name,
            target_match.canonical_name,
            edge.edge_type,
        )
        gold_relation = gold_relation_keys.get(key)
        if gold_relation is None or gold_relation.label == "forbidden":
            unmatched_relations.append(
                f"{source_match.canonical_name} -> {target_match.canonical_name} "
                f"({edge.edge_type})"
            )
        else:
            matched_relation_ids.add(gold_relation.id)

    required_relations = [
        relation for relation in dataset.relations if relation.label == "required"
    ]
    missing_required_relations = [
        relation.id
        for relation in required_relations
        if relation.id not in matched_relation_ids
    ]

    annotator_agreement, annotation_pairs = _annotator_agreement(dataset.nodes)
    metrics = CourseEvaluationMetrics(
        required_recall=_ratio(len(matched_required), len(required)),
        effective_precision=_ratio(
            matched_positive_prediction_count, len(graph.knowledge_nodes)
        ),
        forbidden_leakage_rate=_ratio(
            len(
                {
                    match.id
                    for match in predicted_matches.values()
                    if match.label == "forbidden"
                }
            ),
            len(forbidden),
            empty=0.0,
        ),
        outline_accuracy=_ratio(correct_outline_count, matched_positive_count),
        extractable_outline_coverage=_ratio(
            len(covered_outlines), len(extractable_outlines)
        ),
        evidence_coverage=_ratio(evidence_count, len(graph.knowledge_nodes)),
        duplicate_rate=_ratio(duplicate_count, len(graph.knowledge_nodes), empty=0.0),
        relation_precision=_ratio(len(matched_relation_ids), assessed_relation_count),
        relation_recall=_ratio(
            len(
                {
                    relation.id
                    for relation in required_relations
                    if relation.id in matched_relation_ids
                }
            ),
            len(required_relations),
        ),
        annotator_agreement=annotator_agreement,
        annotation_pairs=annotation_pairs,
        predicted_nodes=len(graph.knowledge_nodes),
        matched_required=len(matched_required),
        required_total=len(required),
        matched_acceptable=len(matched_acceptable),
        forbidden_leaks=len(forbidden_leaks),
        predicted_relations=len(graph.semantic_edges),
        matched_relations=len(matched_relation_ids),
    )
    gates = {
        "required_recall": metrics.required_recall >= thresholds.required_recall,
        "effective_precision": metrics.effective_precision
        >= thresholds.effective_precision,
        "extractable_outline_coverage": metrics.extractable_outline_coverage
        >= thresholds.extractable_outline_coverage,
        "forbidden_leakage_rate": metrics.forbidden_leakage_rate
        <= thresholds.forbidden_leakage_rate,
        "duplicate_rate": metrics.duplicate_rate <= thresholds.duplicate_rate,
        "relation_precision": metrics.relation_precision
        >= thresholds.relation_precision,
        "relation_recall": metrics.relation_recall >= thresholds.relation_recall,
        "annotator_agreement": metrics.annotator_agreement
        >= thresholds.annotator_agreement,
    }
    return CourseEvaluationReport(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        graph_run_id=graph.run_id,
        graph_profile_version=graph.profile_version,
        metrics=metrics,
        thresholds=thresholds,
        gates=gates,
        passed=all(gates.values()),
        missing_required=[
            node.canonical_name
            for node in required
            if node.id not in matched_positive_ids
        ],
        forbidden_leaks=sorted(set(forbidden_leaks)),
        unmatched_predictions=sorted(set(unmatched_predictions)),
        outline_mismatches=sorted(set(outline_mismatches)),
        missing_required_relations=missing_required_relations,
        unmatched_relations=sorted(set(unmatched_relations)),
        unscored_relations=sorted(set(unscored_relations)),
    )


def write_evaluation_report(report: CourseEvaluationReport, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
