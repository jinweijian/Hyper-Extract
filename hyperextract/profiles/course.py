"""Strict, product-neutral course extraction profile contract and compiler."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


KnowledgeLevel = Literal["point", "sub_point"]
KnowledgeKind = Literal[
    "concept", "principle", "method", "process", "tool", "model", "rule"
]
ContentPolicy = Literal["extract", "definitions_only", "skip"]


class KnowledgeExample(_ProfileModel):
    name: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    level: KnowledgeLevel | None = None


class KnowledgePointRules(_ProfileModel):
    definition: str = Field(min_length=1)
    levels: list[KnowledgeLevel] = Field(min_length=2)
    kinds: list[KnowledgeKind] = Field(min_length=1)
    inclusion_rules: list[str] = Field(min_length=1)
    exclusion_rules: list[str] = Field(min_length=1)
    granularity_rules: list[str] = Field(min_length=1)
    positive_examples: list[KnowledgeExample] = Field(min_length=1)
    negative_examples: list[KnowledgeExample] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_values(self) -> "KnowledgePointRules":
        if set(self.levels) != {"point", "sub_point"}:
            raise ValueError("knowledge_points.levels must contain point and sub_point")
        if len(self.kinds) != len(set(self.kinds)):
            raise ValueError("knowledge_points.kinds contains duplicates")
        return self


class RelationExample(_ProfileModel):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class RelationRule(_ProfileModel):
    directed: bool
    definition: str = Field(min_length=1)
    evidence_required: bool = True
    positive_examples: list[RelationExample] = Field(default_factory=list)
    negative_examples: list[RelationExample] = Field(default_factory=list)


class RelationRules(_ProfileModel):
    prerequisite: RelationRule
    derivative: RelationRule
    related: RelationRule
    confusable: RelationRule

    @model_validator(mode="after")
    def validate_directions(self) -> "RelationRules":
        expected = {
            "prerequisite": True,
            "derivative": True,
            "related": False,
            "confusable": False,
        }
        for name, directed in expected.items():
            if getattr(self, name).directed is not directed:
                raise ValueError(f"relations.{name}.directed must be {directed}")
        return self


class ContentPolicies(_ProfileModel):
    body: ContentPolicy
    glossary: ContentPolicy
    index: ContentPolicy
    table_of_contents: ContentPolicy
    header_footer: ContentPolicy
    appendix: ContentPolicy
    references: ContentPolicy

    @model_validator(mode="after")
    def validate_noise_policies(self) -> "ContentPolicies":
        for name in ("index", "table_of_contents", "header_footer", "references"):
            if getattr(self, name) != "skip":
                raise ValueError(f"content_policies.{name} must be skip")
        if self.body != "extract":
            raise ValueError("content_policies.body must be extract")
        return self


class QualityRules(_ProfileModel):
    require_evidence: bool = True
    require_parent_outline: bool = True
    reject_unknown_outline: bool = True
    reject_unknown_endpoints: bool = True
    reject_self_loops: bool = True
    reject_duplicate_edges: bool = True
    reject_relation_without_evidence: bool = True
    canonicalize_undirected_edges: bool = True
    maximum_name_characters: int = Field(default=40, ge=4, le=120)
    minimum_evidence_characters: int = Field(default=4, ge=1, le=100)


class EvaluationThresholds(_ProfileModel):
    required_recall: float = Field(default=0.85, ge=0, le=1)
    effective_precision: float = Field(default=0.90, ge=0, le=1)
    extractable_outline_coverage: float = Field(default=0.85, ge=0, le=1)
    forbidden_leakage_rate: float = Field(default=0.0, ge=0, le=1)
    duplicate_rate: float = Field(default=0.05, ge=0, le=1)
    relation_precision: float = Field(default=0.80, ge=0, le=1)
    relation_recall: float = Field(default=0.75, ge=0, le=1)
    annotator_agreement: float = Field(default=0.80, ge=0, le=1)


class CourseExtractionProfile(_ProfileModel):
    profile_version: Literal[1]
    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    language: str = Field(min_length=2, max_length=16)
    description: str = Field(min_length=1)
    knowledge_points: KnowledgePointRules
    relations: RelationRules
    content_policies: ContentPolicies
    quality_rules: QualityRules
    evaluation_thresholds: EvaluationThresholds = Field(
        default_factory=EvaluationThresholds
    )

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class CompiledCourseProfile:
    name: str
    version: str
    content_hash: str
    nodes: str
    chunk: str
    local_edges: str
    global_edges: str
    dedup: str
    community: str

    @property
    def prompt_hash(self) -> str:
        payload = "\n\x1e\n".join(
            [
                self.nodes,
                self.chunk,
                self.local_edges,
                self.global_edges,
                self.dedup,
                self.community,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def stage(self, name: str) -> str:
        aliases = {
            "nodes": self.nodes,
            "chunk": self.chunk,
            "local-edges": self.local_edges,
            "edges": self.local_edges,
            "global-edges": self.global_edges,
            "dedup": self.dedup,
            "community": self.community,
        }
        try:
            return aliases[name]
        except KeyError as error:
            raise ValueError(f"Unknown profile stage: {name}") from error


DEFAULT_PROFILE_PATH = (
    Path(__file__).parent / "defaults" / "course-knowledge-default.yaml"
)


def load_course_profile(
    path: str | Path | None = None,
) -> CourseExtractionProfile:
    """Load and validate a course profile without initializing model clients."""
    source = Path(path).expanduser().resolve() if path else DEFAULT_PROFILE_PATH
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Course profile must contain a YAML object: {source}")
    return CourseExtractionProfile.model_validate(data)


def _numbered(values: list[str]) -> str:
    return "\n".join(f"{index}. {value}" for index, value in enumerate(values, 1))


def _examples(values: list[KnowledgeExample]) -> str:
    return "\n".join(
        f"- {item.name}: {item.reason}"
        + (f" (level={item.level})" if item.level else "")
        for item in values
    )


def _relation_rules(profile: CourseExtractionProfile) -> str:
    lines: list[str] = []
    for name in ("prerequisite", "derivative", "related", "confusable"):
        rule = getattr(profile.relations, name)
        direction = "directed" if rule.directed else "undirected"
        lines.append(f"- {name} ({direction}): {rule.definition}")
        for example in rule.positive_examples:
            lines.append(
                f"  positive: {example.source} -> {example.target}; {example.reason}"
            )
        for example in rule.negative_examples:
            lines.append(
                f"  negative: {example.source} -> {example.target}; {example.reason}"
            )
    return "\n".join(lines)


def compile_course_profile(profile: CourseExtractionProfile) -> CompiledCourseProfile:
    """Compile one semantic source of truth into every course extraction stage."""
    rules = profile.knowledge_points
    nodes = f"""
你是课程知识图谱抽取专家。知识点定义：{rules.definition}

抽取顺序：
{_numbered(rules.inclusion_rules)}

排除规则：
{_numbered(rules.exclusion_rules)}

颗粒度规则：
{_numbered(rules.granularity_rules)}

正例：
{_examples(rules.positive_examples)}

反例：
{_examples(rules.negative_examples)}

输出约束：
1. level 只能为 point 或 sub_point；knowledge_kind 只能来自 {", ".join(rules.kinds)}。
2. 名称应短、稳定，不能使用完整句子，最长 {profile.quality_rules.maximum_name_characters} 个字符。
3. summary 必须说明知识内涵；evidence 必须是当前正文中的直接依据。
4. parent_outline_id 必须从当前块覆盖章节中选择最贴近的目录 ID。
5. 内容不足时允许输出零个知识点，禁止为了数量而拆词或拆句。
6. source_refs 和 id 留空，系统根据输入包来源补齐。
7. 不生成考频、教学难度、课程重要度或其他无直接来源的推断属性。

### 文档上下文
{{source_text}}
""".strip()

    relation_text = _relation_rules(profile)
    local_edges = f"""
你是课程知识关系抽取专家。只在给定知识点之间抽取有文本依据的教学关系。

关系规则：
{relation_text}

严格规则：
1. source 和 target 必须逐字使用给定知识点名称。
2. contains/describes 由系统生成，模型不得输出结构关系。
3. 仅主题相近、出现在同一章节或可以一起讨论，都不足以创建 related。
4. prerequisite 必须是理解目标的必要学习依赖；derivative 必须说明应用、推广或进阶路径。
5. confusable 必须同时说明相似点和关键区别。
6. 无证据就不输出；status 固定为 pending，source_refs 留空。

### 给定知识点
{{known_nodes}}

### 文档上下文
{{source_text}}
""".strip()

    chunk = f"""
你是课程知识图谱抽取专家。请在同一次输出中先抽取知识点，再仅在这些知识点之间抽取有直接依据的局部教学关系。

知识点定义：{rules.definition}

知识点纳入规则：
{_numbered(rules.inclusion_rules)}

知识点排除规则：
{_numbered(rules.exclusion_rules)}

颗粒度规则：
{_numbered(rules.granularity_rules)}

关系规则：
{relation_text}

严格规则：
1. 按当前块覆盖目录逐一判断抽取或跳过，避免因多个目录合并而遗漏；任何目录都可以没有直属知识点。
2. 不得为了数量拆词、拆句或仅复制目录标题；标题所指概念被正文实质定义时，应使用规范概念名抽取。
3. 每个节点必须给出短名称、最贴近的 parent_outline_id、可独立理解的 summary 和直接 evidence。
4. edges 只能引用同一次输出 nodes 中逐字一致的名称。
5. contains/describes 由系统生成；仅主题相近、同章出现或业务上有关都不能创建 related。
6. 正文明确对照两个相似概念并说明区别时，优先使用 confusable，不要降级为 related。
7. prerequisite 必须是必要学习依赖；derivative 必须是明确应用或进阶；confusable 必须说明共同点和区别。
8. source_refs 和 id 留空；不生成考频、难度或重要度。

### 文档上下文
{{source_text}}
""".strip()

    global_edges = f"""
你负责判断课程知识图谱的跨章节候选关系，只能判断给定候选对。

关系规则：
{relation_text}

严格规则：
1. 不得创建候选列表外的知识点或关系类型。
2. 仅主题相近不足以生成 related；默认应当拒绝关系。
3. 每条保留关系必须给出具体教学依赖、发展路径或辨析依据。
4. 证据明确说明一个策略或知识点“包括”另一个流程或实现时，使用 derivative，方向为上级策略到具体实现。
5. 证据明确对照两个同层维度并说明区别时，使用 confusable，不要降级为 related。
6. 如果判断说明中出现“未明确关系”“仅主题相近”“平行概念”或同等否定结论，必须拒绝该边，不得一边否定一边输出。
7. status 固定为 pending，source_refs 留空。

### 候选知识点对
{{candidates}}
""".strip()

    dedup = """
判断两个课程知识点是否内涵完全相同、只是措辞或别名不同。
上下位、组成、相关、前置、应用、部分重叠或同属一节，都不是同一知识点。
如果完全相同，preferred_name 选择更规范且适合作为课程目录的名称；否则 same=false 且 preferred_name 留空。

A: {left}
B: {right}
""".strip()
    community = """
根据给定课程知识点及已经通过质量门的教学关系，生成简短主题名称和摘要。
不得引入列表外的知识，不得把社区主题写回为知识点。

知识点：
{nodes}

关系：
{edges}
""".strip()
    return CompiledCourseProfile(
        name=profile.name,
        version=profile.version,
        content_hash=profile.content_hash,
        nodes=nodes,
        chunk=chunk,
        local_edges=local_edges,
        global_edges=global_edges,
        dedup=dedup,
        community=community,
    )


def profile_summary(profile: CourseExtractionProfile) -> dict[str, object]:
    """Return a stable CLI/report representation."""
    return {
        "name": profile.name,
        "version": profile.version,
        "profile_version": profile.profile_version,
        "language": profile.language,
        "content_hash": profile.content_hash,
        "knowledge_levels": profile.knowledge_points.levels,
        "knowledge_kinds": profile.knowledge_points.kinds,
        "relation_types": [
            "prerequisite",
            "derivative",
            "related",
            "confusable",
        ],
    }


def normalize_profile_name(value: str) -> str:
    """Normalize labels for deterministic evaluator matching."""
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value.lower())
