"""Resumable DoclingDocument-to-course-graph pipeline."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, TypeVar

import yaml

from hyperextract.methods.rag.course_knowledge_graph import (
    COURSE_PROFILE_VERSION,
    CommunityReport,
    CourseEdge,
    CourseEdgeList,
    CourseChunkResult,
    CourseKnowledgeGraph,
    CourseNode,
    DedupDecision,
    normalize_name,
    stable_node_id,
)
from hyperextract.profiles.course import CourseExtractionProfile, load_course_profile
from hyperextract.providers.contracts import CanonicalModelFailure
from hyperextract.providers.gateway import GatewayExecutionError
from hyperextract.providers.profiles import ProfileRecovery
from hyperextract.providers.recovery import RecoveryPolicy, RecoveryState

from .checkpoint import RunCheckpoint, atomic_write_json, atomic_write_text, fingerprint
from .context_planner import ContextBudget, ContextBudgetError
from .course_graph import (
    CourseKnowledgeNodeV1,
    CourseSemanticEdgeV1,
    build_course_graph_v1,
)
from .docling import (
    estimate_tokens,
    load_docling_document,
    plan_document_chunks,
    render_chunk_context,
)
from .document_package import (
    document_package_fingerprint,
    load_document_package,
    load_package_extraction_brief,
)
from .model_errors import (
    ContextWindowExceededError,
    OutputTruncatedError,
    classify_model_error,
)
from .models import DocumentChunk, DocumentOutline, SourceReference
from .run_reports import build_cost_report, build_performance_report


T = TypeVar("T")


@dataclass(frozen=True)
class PipelineOptions:
    target_tokens: int = 4000
    max_tokens: int = 6000
    max_workers: int = 2
    retry_attempts: int = 4
    recovery: ProfileRecovery | None = None
    heartbeat_interval: int = 30
    semantic_dedup: bool = True
    community_reports: bool = False
    build_index: bool = False
    model_context_tokens: int = 32768
    output_reserve_tokens: int = 8192
    prompt_reserve_tokens: int = 2500
    schema_reserve_tokens: int = 2500
    combined_local_extraction: bool = False
    global_edge_top_k: int = 1
    global_edge_similarity_threshold: float = 0.70


@dataclass(frozen=True)
class PipelineControl:
    run_id: str | None = None
    event_sink: Callable[[Any], None] | None = None
    should_cancel: Callable[[], bool] = lambda: False


class RunCancelled(RuntimeError):
    """Raised at a checkpoint-safe boundary after cancellation is requested."""


def _with_model_request_metadata(
    ka: CourseKnowledgeGraph,
    operation: Callable[[], T],
    **metadata: str | None,
) -> T:
    context = getattr(ka, "model_request_context", None)
    manager = context(**metadata) if callable(context) else nullcontext()
    with manager:
        return operation()


def _check_cancel(control: PipelineControl | None) -> None:
    if control is not None and control.should_cancel():
        raise RunCancelled("Run cancellation requested")


@dataclass(frozen=True)
class GlobalEdgeCandidate:
    left: CourseNode
    right: CourseNode
    similarity: float
    selection_reasons: tuple[str, ...]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _optional_non_negative_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    value = float(raw)
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _resolve_input_format(source: Path, input_format: str) -> str:
    if input_format not in {"auto", "docling-json", "document-package"}:
        raise ValueError(
            f"Unsupported structured document input format: {input_format}"
        )
    if input_format != "auto":
        return input_format
    if source.is_dir() and (source / "manifest.json").is_file():
        return "document-package"
    if source.is_file() and source.suffix.lower() == ".json":
        return "docling-json"
    raise ValueError("Cannot infer structured document format; use --input-format")


def _migrate_compatible_profile_checkpoint(
    output: Path,
    *,
    source_hash: str,
    config: dict[str, Any],
) -> None:
    manifest_path = output / ".he-run" / "run.json"
    existing = RunCheckpoint.read_json(manifest_path)
    if not existing or existing.get("source_fingerprint") != source_hash:
        return
    old_config = existing.get("config")
    if not isinstance(old_config, dict):
        return
    changed = {
        key
        for key in set(old_config) | set(config)
        if old_config.get(key) != config.get(key)
    }
    if changed != {"profile_hash"}:
        return
    if old_config.get("method_fingerprint") != config.get("method_fingerprint"):
        return
    existing["config"] = config
    existing["fingerprint"] = fingerprint({"source": source_hash, "config": config})
    atomic_write_json(manifest_path, existing)


def _is_size_error(error: Exception) -> bool:
    return isinstance(
        error,
        (ContextWindowExceededError, ContextBudgetError, OutputTruncatedError),
    )


def _retry(
    operation: Callable[[], T],
    *,
    checkpoint: RunCheckpoint,
    stage: str,
    message: str,
    attempts: int,
    recovery: ProfileRecovery | None = None,
    heartbeat_interval: int,
    chunk_id: str | None = None,
) -> T:
    state = RecoveryState()
    policy = RecoveryPolicy(
        recovery
        or ProfileRecovery(
            transient_retry_attempts=max(0, attempts - 1),
            validation_repair_attempts=0,
            validation_retry_attempts=0,
        )
    )
    recovery_started = time.monotonic()
    attempt = 1
    while True:
        try:
            with checkpoint.heartbeat(
                stage,
                message,
                interval=heartbeat_interval,
                chunk_id=chunk_id,
            ):
                return operation()
        except Exception as error:
            classified = classify_model_error(error)
            if _contains_gateway_execution_error(classified):
                raise classified from error
            failure = _canonical_pipeline_failure(
                classified,
                request_id=f"{stage}:{chunk_id or 'run'}:{attempt}",
            )
            state.rate_limit_elapsed_seconds = time.monotonic() - recovery_started
            decision = policy.decide(failure, state)
            if decision.action != "retry":
                raise classified from error
            if failure.category.startswith("rate_limit."):
                state.rate_limit_attempts += 1
            else:
                state.transient_attempts += 1
            attempt += 1
            checkpoint.emit(
                stage,
                "retrying",
                f"{message}失败，{decision.delay_seconds:.1f}s 后重试："
                f"{classified.category}: {classified}",
                chunk_id=chunk_id,
                attempt=attempt,
                recovery_action=decision.action,
                recovery_target=decision.target,
                recovery_reason=decision.reason,
            )
            time.sleep(decision.delay_seconds)


def _contains_gateway_execution_error(error: BaseException) -> bool:
    """Return whether provider recovery was already exhausted by the gateway."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, GatewayExecutionError):
            return True
        seen.add(id(current))
        original = getattr(current, "original", None)
        current = original if isinstance(original, BaseException) else current.__cause__
    return False


def _canonical_pipeline_failure(
    error: Exception, *, request_id: str
) -> CanonicalModelFailure:
    category = getattr(error, "category", "model_error")
    mapped = {
        "rate_limit": "rate_limit.requests",
        "unsupported_capability": "unsupported_capability",
        "authentication": "authentication",
        "context_window": "context_window",
        "output_truncated": "output_truncated",
        "output_validation": "output_validation",
        "transient": "transient",
    }.get(category, "unknown")
    return CanonicalModelFailure(
        request_id=request_id,
        category=mapped,
        reason=category,
        raw_message=str(error),
    )


def _render_budgeted_context(
    outline: DocumentOutline,
    chunk: DocumentChunk,
    known_terms: list[str],
    options: PipelineOptions,
) -> str:
    limited_terms = list(dict.fromkeys(known_terms))[-24:]
    attempts = ((limited_terms, True), ([], True), ([], False))
    last_error: ContextBudgetError | None = None
    for terms, compact_outline in attempts:
        context = render_chunk_context(
            outline,
            chunk,
            known_terms=terms,
            compact_outline=compact_outline,
        )
        content_tokens = estimate_tokens(chunk.text)
        rendered_overhead = max(0, estimate_tokens(context) - content_tokens)
        budget = ContextBudget(
            context_window=options.model_context_tokens,
            output_reserve=options.output_reserve_tokens,
            prompt_tokens=options.prompt_reserve_tokens,
            schema_tokens=options.schema_reserve_tokens,
            outline_tokens=rendered_overhead,
            known_terms_tokens=0,
        )
        try:
            budget.ensure_fits(content_tokens=content_tokens)
            return context
        except ContextBudgetError as error:
            last_error = error
    assert last_error is not None
    raise last_error


def _outline_title_key(title: str) -> str:
    return normalize_name(re.sub(r"^\s*\d+(?:\.\d+)*\s*", "", title).strip())


def _normalize_chunk_graph(
    graph: Any,
    chunk: DocumentChunk,
    outline: DocumentOutline,
) -> dict[str, Any]:
    outline_ids = set(chunk.covered_outline_ids or [chunk.outline_id])
    outline_by_name: dict[str, list[str]] = {}
    for item in outline.nodes:
        if item.id not in outline_ids:
            continue
        outline_by_name.setdefault(_outline_title_key(item.title), []).append(item.id)
    ref_ids = [ref.ref for ref in chunk.source_refs]
    nodes: list[CourseNode] = []
    aliases: dict[str, str] = {}
    for raw in graph.nodes:
        name = raw.name.strip()
        key = normalize_name(name)
        if not key:
            continue
        parent = (
            raw.parent_outline_id
            if raw.parent_outline_id in outline_ids
            else chunk.outline_id
        )
        exact_outline_matches = outline_by_name.get(key, [])
        if len(exact_outline_matches) == 1:
            parent = exact_outline_matches[0]
        node = raw.model_copy(
            update={
                "id": stable_node_id(name),
                "name": name,
                "parent_outline_id": parent,
                "source_refs": sorted(set(raw.source_refs + ref_ids)),
                "appearances": sorted(set(raw.appearances + [parent])),
            }
        )
        aliases[key] = name
        nodes.append(node)

    known = {node.name for node in nodes}
    edges: list[CourseEdge] = []
    for raw in graph.edges:
        source = aliases.get(normalize_name(raw.source), raw.source.strip())
        target = aliases.get(normalize_name(raw.target), raw.target.strip())
        if source not in known or target not in known or source == target:
            continue
        edges.append(
            raw.model_copy(
                update={
                    "source": source,
                    "target": target,
                    "status": "pending",
                    "source_refs": sorted(set(raw.source_refs + ref_ids)),
                }
            )
        )
    return {
        "nodes": [node.model_dump() for node in nodes],
        "edges": [edge.model_dump() for edge in edges],
    }


def _split_chunk_text(
    chunk: DocumentChunk,
) -> tuple[DocumentChunk, DocumentChunk] | None:
    paragraphs = [
        part.strip() for part in re.split(r"\n\s*\n", chunk.text) if part.strip()
    ]
    if len(paragraphs) < 2:
        return None
    midpoint = len(paragraphs) // 2
    left_text = "\n\n".join(paragraphs[:midpoint])
    right_text = "\n\n".join(paragraphs[midpoint:])
    left = chunk.model_copy(update={"id": f"{chunk.id}-a", "text": left_text})
    right = chunk.model_copy(update={"id": f"{chunk.id}-b", "text": right_text})
    return left, right


def _extract_local_edge_batch(
    ka: CourseKnowledgeGraph,
    context: str,
    nodes: list[CourseNode],
    batch_path: Path,
    checkpoint: RunCheckpoint,
    options: PipelineOptions,
    *,
    chunk_id: str,
    label: str,
) -> list[CourseEdge]:
    cached = RunCheckpoint.read_json(batch_path)
    if cached:
        return [CourseEdge.model_validate(value) for value in cached.get("items", [])]
    try:
        edges = _retry(
            lambda: _with_model_request_metadata(
                ka,
                lambda: ka.extract_edges(context, nodes),
                chunk_id=chunk_id,
                batch_id=batch_path.stem,
            ),
            checkpoint=checkpoint,
            stage="local_extract",
            message=label,
            attempts=options.retry_attempts,
            recovery=options.recovery,
            heartbeat_interval=options.heartbeat_interval,
            chunk_id=chunk_id,
        )
    except OutputTruncatedError:
        if len(nodes) <= 1:
            raise
        midpoint = len(nodes) // 2
        checkpoint.emit(
            "local_extract",
            "progress",
            f"{label} 输出截断，自动拆分 {len(nodes)} 个节点为 {midpoint}+{len(nodes) - midpoint}",
            chunk_id=chunk_id,
            original_batch_size=len(nodes),
        )
        left = _extract_local_edge_batch(
            ka,
            context,
            nodes[:midpoint],
            batch_path.with_name(f"{batch_path.stem}-a.json"),
            checkpoint,
            options,
            chunk_id=chunk_id,
            label=f"{label} 子批次 A",
        )
        right = _extract_local_edge_batch(
            ka,
            context,
            nodes[midpoint:],
            batch_path.with_name(f"{batch_path.stem}-b.json"),
            checkpoint,
            options,
            chunk_id=chunk_id,
            label=f"{label} 子批次 B",
        )
        edges = left + right
    atomic_write_json(
        batch_path,
        {"items": [edge.model_dump() for edge in edges]},
    )
    return edges


def _extract_chunk(
    ka: CourseKnowledgeGraph,
    outline: DocumentOutline,
    chunk: DocumentChunk,
    known_terms: list[str],
    checkpoint: RunCheckpoint,
    options: PipelineOptions,
    *,
    depth: int = 0,
) -> dict[str, Any]:
    context = _render_budgeted_context(outline, chunk, known_terms, options)
    chunk_dir = checkpoint.chunk_dir(chunk.id)
    try:
        if options.combined_local_extraction and hasattr(ka, "extract_chunk_result"):
            cached_result = RunCheckpoint.read_json(chunk_dir / "chunk-result.json")
            if cached_result:
                combined = CourseChunkResult.model_validate(cached_result)
                checkpoint.emit(
                    "local_extract",
                    "progress",
                    f"恢复 {chunk.id} 节点与局部关系结果",
                    chunk_id=chunk.id,
                )
            else:
                combined = _retry(
                    lambda: _with_model_request_metadata(
                        ka,
                        lambda: ka.extract_chunk_result(context),
                        chunk_id=chunk.id,
                        batch_id="chunk-result",
                    ),
                    checkpoint=checkpoint,
                    stage="local_extract",
                    message=f"节点与局部关系联合抽取 {chunk.id}",
                    attempts=options.retry_attempts,
                    recovery=options.recovery,
                    heartbeat_interval=options.heartbeat_interval,
                    chunk_id=chunk.id,
                )
                atomic_write_json(
                    chunk_dir / "chunk-result.json", combined.model_dump()
                )
                checkpoint.emit(
                    "local_extract",
                    "progress",
                    f"{chunk.id} 联合抽取完成：{len(combined.nodes)} 节点 / {len(combined.edges)} 关系",
                    chunk_id=chunk.id,
                )
            graph = ka.graph_schema(nodes=combined.nodes, edges=combined.edges)
            return _normalize_chunk_graph(graph, chunk, outline)

        cached_nodes = RunCheckpoint.read_json(chunk_dir / "nodes.json")
        if cached_nodes:
            nodes = [
                CourseNode.model_validate(value)
                for value in cached_nodes.get("items", [])
            ]
            checkpoint.emit(
                "local_extract",
                "progress",
                f"恢复 {chunk.id} 节点结果",
                chunk_id=chunk.id,
            )
        else:
            nodes = _retry(
                lambda: _with_model_request_metadata(
                    ka,
                    lambda: ka.extract_nodes(context),
                    chunk_id=chunk.id,
                    batch_id="nodes",
                ),
                checkpoint=checkpoint,
                stage="local_extract",
                message=f"节点抽取 {chunk.id}",
                attempts=options.retry_attempts,
                recovery=options.recovery,
                heartbeat_interval=options.heartbeat_interval,
                chunk_id=chunk.id,
            )
            atomic_write_json(
                chunk_dir / "nodes.json",
                {"items": [node.model_dump() for node in nodes]},
            )
            checkpoint.emit(
                "local_extract",
                "progress",
                f"{chunk.id} 节点抽取完成：{len(nodes)} 个",
                chunk_id=chunk.id,
            )

        cached_edges = RunCheckpoint.read_json(chunk_dir / "local-edges.json")
        if cached_edges:
            edges = [
                CourseEdge.model_validate(value)
                for value in cached_edges.get("items", [])
            ]
            checkpoint.emit(
                "local_extract",
                "progress",
                f"恢复 {chunk.id} 局部关系结果",
                chunk_id=chunk.id,
            )
        else:
            edges = []
            edge_batches = [
                nodes[index : index + 12] for index in range(0, len(nodes), 12)
            ]
            for batch_index, batch_nodes in enumerate(edge_batches):
                batch_path = (
                    chunk_dir / "local-edge-batches" / f"batch-{batch_index:04d}.json"
                )
                cached_batch = RunCheckpoint.read_json(batch_path)
                if cached_batch:
                    batch_edges = [
                        CourseEdge.model_validate(value)
                        for value in cached_batch.get("items", [])
                    ]
                else:
                    batch_edges = _extract_local_edge_batch(
                        ka,
                        context,
                        batch_nodes,
                        batch_path,
                        checkpoint,
                        options,
                        chunk_id=chunk.id,
                        label=(
                            f"关系抽取 {chunk.id} 批次 {batch_index + 1}/{len(edge_batches)}"
                        ),
                    )
                edges.extend(batch_edges)
            atomic_write_json(
                chunk_dir / "local-edges.json",
                {"items": [edge.model_dump() for edge in edges]},
            )
            checkpoint.emit(
                "local_extract",
                "progress",
                f"{chunk.id} 关系抽取完成：{len(edges)} 条",
                chunk_id=chunk.id,
            )
        graph = ka.graph_schema(nodes=nodes, edges=edges)
        return _normalize_chunk_graph(graph, chunk, outline)
    except Exception as error:
        split = _split_chunk_text(chunk)
        if depth < 2 and split and _is_size_error(error):
            checkpoint.emit(
                "local_extract",
                "progress",
                f"{chunk.id} 持续失败，按段落边界拆为两个子块",
                chunk_id=chunk.id,
                error=str(error),
            )
            left = _extract_chunk(
                ka, outline, split[0], known_terms, checkpoint, options, depth=depth + 1
            )
            right = _extract_chunk(
                ka, outline, split[1], known_terms, checkpoint, options, depth=depth + 1
            )
            return {
                "nodes": left["nodes"] + right["nodes"],
                "edges": left["edges"] + right["edges"],
            }
        raise


def _merge_node(existing: CourseNode, incoming: CourseNode) -> CourseNode:
    summary = (
        incoming.summary
        if len(incoming.summary) > len(existing.summary)
        else existing.summary
    )
    evidence = (
        incoming.evidence
        if len(incoming.evidence) > len(existing.evidence)
        else existing.evidence
    )
    return existing.model_copy(
        update={
            "summary": summary,
            "evidence": evidence,
            "aliases": sorted(set(existing.aliases + incoming.aliases)),
            "confidence": max(existing.confidence, incoming.confidence),
            "source_refs": sorted(set(existing.source_refs + incoming.source_refs)),
            "appearances": sorted(set(existing.appearances + incoming.appearances)),
        }
    )


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _embedding_cosine(
    embeddings: list[list[float] | None], left: int, right: int
) -> float:
    if (
        not embeddings
        or left >= len(embeddings)
        or right >= len(embeddings)
        or embeddings[left] is None
        or embeddings[right] is None
    ):
        return 0.0
    return _cosine(embeddings[left], embeddings[right])


def _embed_documents_with_quarantine(
    embedder: Any, texts: list[str]
) -> list[list[float] | None]:
    embed_with_status = getattr(embedder, "embed_with_status", None)
    if callable(embed_with_status):
        response = embed_with_status(texts)
        return [item.vector for item in response.items]
    return embedder.embed_documents(texts)


def _deduplicate(
    ka: CourseKnowledgeGraph,
    graphs: list[dict[str, Any]],
    checkpoint: RunCheckpoint,
    options: PipelineOptions,
) -> tuple[
    list[CourseNode],
    list[CourseEdge],
    list[dict[str, Any]],
    list[list[float] | None],
]:
    grouped: dict[str, CourseNode] = {}
    aliases: dict[str, str] = {}
    log: list[dict[str, Any]] = []
    raw_edges: list[CourseEdge] = []
    for graph in graphs:
        for value in graph.get("nodes", []):
            node = CourseNode.model_validate(value)
            key = normalize_name(node.name)
            if key in grouped:
                before = grouped[key]
                grouped[key] = _merge_node(before, node)
                log.append({"kind": "exact", "from": node.name, "to": before.name})
            else:
                grouped[key] = node
            aliases[key] = grouped[key].name
        raw_edges.extend(
            CourseEdge.model_validate(value) for value in graph.get("edges", [])
        )

    nodes = list(grouped.values())
    embeddings: list[list[float] | None] = []
    if nodes:
        embeddings = _retry(
            lambda: _embed_documents_with_quarantine(
                ka.embedder, [f"{node.name}\n{node.summary}" for node in nodes]
            ),
            checkpoint=checkpoint,
            stage="deduplicate",
            message="生成知识点去重向量",
            attempts=options.retry_attempts,
            recovery=options.recovery,
            heartbeat_interval=options.heartbeat_interval,
        )

    if options.semantic_dedup and len(nodes) > 1:
        candidates: list[tuple[float, int, int]] = []
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                lexical = SequenceMatcher(
                    None, normalize_name(nodes[i].name), normalize_name(nodes[j].name)
                ).ratio()
                similarity = _embedding_cosine(embeddings, i, j)
                if similarity >= 0.94 or lexical >= 0.82:
                    candidates.append((max(similarity, lexical), i, j))
        parent = list(range(len(nodes)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        for score, left_index, right_index in sorted(candidates, reverse=True)[:200]:
            if find(left_index) == find(right_index):
                continue
            left, right = nodes[left_index], nodes[right_index]
            decision_path = (
                checkpoint.root
                / "stages"
                / "dedup-decisions"
                / f"{fingerprint(sorted((left.id, right.id)))[:20]}.json"
            )
            cached_decision = RunCheckpoint.read_json(decision_path)
            if cached_decision:
                decision = DedupDecision.model_validate(cached_decision)
            else:
                decision = _retry(
                    lambda left=left, right=right: _with_model_request_metadata(
                        ka,
                        lambda: ka.dedup_extractor.invoke(
                            {
                                "left": f"{left.name}: {left.summary}",
                                "right": f"{right.name}: {right.summary}",
                            }
                        ),
                        batch_id=decision_path.stem,
                    ),
                    checkpoint=checkpoint,
                    stage="deduplicate",
                    message=f"判断同义知识点：{left.name} / {right.name}",
                    attempts=options.retry_attempts,
                    recovery=options.recovery,
                    heartbeat_interval=options.heartbeat_interval,
                )
                if decision is not None:
                    atomic_write_json(decision_path, decision.model_dump())
            if decision and decision.same:
                left_root, right_root = find(left_index), find(right_index)
                parent[right_root] = left_root
                nodes[left_root] = _merge_node(nodes[left_root], nodes[right_root])
                aliases[normalize_name(nodes[right_root].name)] = nodes[left_root].name
                log.append(
                    {
                        "kind": "semantic",
                        "from": nodes[right_root].name,
                        "to": nodes[left_root].name,
                        "score": round(score, 4),
                        "reason": decision.reason,
                    }
                )
        nodes = [node for index, node in enumerate(nodes) if find(index) == index]
        if nodes:
            embeddings = _retry(
                lambda: _embed_documents_with_quarantine(
                    ka.embedder, [f"{node.name}\n{node.summary}" for node in nodes]
                ),
                checkpoint=checkpoint,
                stage="deduplicate",
                message="刷新合并后的知识点向量",
                attempts=options.retry_attempts,
                recovery=options.recovery,
                heartbeat_interval=options.heartbeat_interval,
            )

    canonical = {normalize_name(node.name): node.name for node in nodes}
    canonical.update(
        {
            key: canonical.get(normalize_name(value), value)
            for key, value in aliases.items()
        }
    )
    valid = {node.name for node in nodes}
    edges_by_key: dict[tuple[str, str, str], CourseEdge] = {}
    for edge in raw_edges:
        source = canonical.get(normalize_name(edge.source), edge.source)
        target = canonical.get(normalize_name(edge.target), edge.target)
        if source not in valid or target not in valid or source == target:
            continue
        if edge.edge_type in {"related", "confusable"} and source > target:
            source, target = target, source
        key = (source, target, edge.edge_type)
        normalized = edge.model_copy(update={"source": source, "target": target})
        previous = edges_by_key.get(key)
        if previous is None or normalized.confidence > previous.confidence:
            edges_by_key[key] = normalized
    return nodes, list(edges_by_key.values()), log, embeddings


def _global_edge_candidates(
    outline: DocumentOutline,
    nodes: list[CourseNode],
    embeddings: list[list[float] | None],
    *,
    existing_edges: list[CourseEdge] | None = None,
    top_k: int = 1,
    similarity_threshold: float = 0.78,
) -> list[GlobalEdgeCandidate]:
    node_map = outline.node_map()

    def top_level(outline_id: str) -> str | None:
        cursor = node_map.get(outline_id)
        while cursor and cursor.level > 1 and cursor.parent_id:
            cursor = node_map.get(cursor.parent_id)
        return cursor.id if cursor and cursor.level == 1 else None

    node_index = {normalize_name(node.name): index for index, node in enumerate(nodes)}
    resolved_pairs = {
        tuple(sorted((node_index[source], node_index[target])))
        for edge in (existing_edges or [])
        if (source := normalize_name(edge.source)) in node_index
        and (target := normalize_name(edge.target)) in node_index
    }

    def explicit_mention(left: CourseNode, right: CourseNode) -> bool:
        ignored = {
            "项目",
            "管理",
            "团队",
            "职能",
            "流程",
            "知识",
            "系统",
            "方式",
            "能力",
            "相关",
            "提供",
        }
        compact_name = re.sub(r"[^\w\u4e00-\u9fff]", "", left.name)
        terms = {
            compact_name[start : start + size]
            for size in (2, 3, 4)
            for start in range(max(0, len(compact_name) - size + 1))
        } - ignored
        haystack = f"{right.name}\n{right.summary}\n{right.evidence}"
        return any(term in haystack for term in terms)

    pairs: dict[tuple[int, int], tuple[float, set[str]]] = {}
    for i, node in enumerate(nodes):
        scored: list[tuple[float, int, set[str]]] = []
        for j, other in enumerate(nodes):
            if i == j:
                continue
            pair_key = tuple(sorted((i, j)))
            if pair_key in resolved_pairs:
                continue
            if top_level(node.parent_outline_id) != top_level(other.parent_outline_id):
                continue
            score = _embedding_cosine(embeddings, i, j)
            reasons: set[str] = set()
            if score >= similarity_threshold:
                reasons.add("semantic_similarity")
            if node.parent_outline_id == other.parent_outline_id:
                reasons.add("same_section_gap")
            if explicit_mention(node, other) or explicit_mention(other, node):
                reasons.add("explicit_term_mention")
            if reasons:
                scored.append((score, j, reasons))
        for score, j, reasons in sorted(scored, reverse=True)[: max(0, top_k)]:
            key = tuple(sorted((i, j)))
            previous_score, previous_reasons = pairs.get(key, (0.0, set()))
            pairs[key] = (max(score, previous_score), previous_reasons | reasons)

    by_section: dict[str, list[int]] = {}
    for index, node in enumerate(nodes):
        by_section.setdefault(node.parent_outline_id, []).append(index)
    for section_nodes in by_section.values():
        contrast: tuple[float, int, int] | None = None
        for position, i in enumerate(section_nodes):
            for j in section_nodes[position + 1 :]:
                key = tuple(sorted((i, j)))
                if key in resolved_pairs:
                    continue
                left_name = normalize_name(nodes[i].name)
                right_name = normalize_name(nodes[j].name)
                name_similarity = SequenceMatcher(None, left_name, right_name).ratio()
                if name_similarity < 0.45:
                    continue
                common_suffix = 0
                for left_char, right_char in zip(
                    reversed(left_name), reversed(right_name), strict=False
                ):
                    if left_char != right_char:
                        break
                    common_suffix += 1
                contrast_score = name_similarity + min(0.6, common_suffix * 0.2)
                if contrast is None or contrast_score > contrast[0]:
                    contrast = (contrast_score, i, j)
        if contrast:
            _, i, j = contrast
            key = tuple(sorted((i, j)))
            score = _embedding_cosine(embeddings, i, j)
            previous_score, previous_reasons = pairs.get(key, (0.0, set()))
            pairs[key] = (
                max(score, previous_score),
                previous_reasons | {"same_section_contrast"},
            )
    return [
        GlobalEdgeCandidate(
            left=nodes[i],
            right=nodes[j],
            similarity=score,
            selection_reasons=tuple(sorted(reasons)),
        )
        for (i, j), (score, reasons) in sorted(
            pairs.items(), key=lambda item: item[1][0], reverse=True
        )
    ]


def _generate_global_edges(
    ka: CourseKnowledgeGraph,
    outline: DocumentOutline,
    nodes: list[CourseNode],
    embeddings: list[list[float] | None],
    existing_edges: list[CourseEdge],
    checkpoint: RunCheckpoint,
    options: PipelineOptions,
) -> list[CourseEdge]:
    candidates = _global_edge_candidates(
        outline,
        nodes,
        embeddings,
        existing_edges=existing_edges,
        top_k=options.global_edge_top_k,
        similarity_threshold=options.global_edge_similarity_threshold,
    )
    atomic_write_json(
        checkpoint.root / "stages" / "global-edge-candidates.json",
        {
            "count": len(candidates),
            "top_k": options.global_edge_top_k,
            "similarity_threshold": options.global_edge_similarity_threshold,
            "items": [
                {
                    "source": candidate.left.name,
                    "target": candidate.right.name,
                    "similarity": round(candidate.similarity, 6),
                    "selection_reasons": list(candidate.selection_reasons),
                }
                for candidate in candidates
            ],
        },
    )
    generated: list[CourseEdge] = []
    for offset in range(0, len(candidates), 40):
        batch = candidates[offset : offset + 40]
        batch_path = (
            checkpoint.root
            / "stages"
            / "global-edge-batches"
            / f"batch-{offset // 40:05d}.json"
        )
        cached_batch = RunCheckpoint.read_json(batch_path)
        if cached_batch:
            generated.extend(
                CourseEdge.model_validate(value)
                for value in cached_batch.get("items", [])
            )
            checkpoint.emit(
                "global_edges",
                "progress",
                f"恢复跨章关系候选 {offset + 1}-{offset + len(batch)} / {len(candidates)}",
            )
            continue
        batch_edges: list[CourseEdge] = []
        for part_offset in range(0, len(batch), 12):
            part = batch[part_offset : part_offset + 12]
            part_path = batch_path.with_name(
                f"{batch_path.stem}-part-{part_offset // 12:03d}.json"
            )
            cached_part = RunCheckpoint.read_json(part_path)
            if cached_part:
                part_edges = [
                    CourseEdge.model_validate(value)
                    for value in cached_part.get("items", [])
                ]
            else:
                text = "\n".join(
                    f"- A={candidate.left.name}（summary={candidate.left.summary}; "
                    f"evidence={candidate.left.evidence}） | "
                    f"B={candidate.right.name}（summary={candidate.right.summary}; "
                    f"evidence={candidate.right.evidence}） | "
                    f"similarity={candidate.similarity:.3f} | "
                    f"selected_by={','.join(candidate.selection_reasons)}"
                    for candidate in part
                )
                absolute_start = offset + part_offset + 1
                absolute_end = absolute_start + len(part) - 1
                result = _retry(
                    lambda text=text: _with_model_request_metadata(
                        ka,
                        lambda: ka.global_edge_extractor.invoke({"candidates": text}),
                        batch_id=part_path.stem,
                    ),
                    checkpoint=checkpoint,
                    stage="global_edges",
                    message=(
                        f"生成跨章关系候选 {absolute_start}-{absolute_end} / {len(candidates)}"
                    ),
                    attempts=options.retry_attempts,
                    recovery=options.recovery,
                    heartbeat_interval=options.heartbeat_interval,
                )
                part_edges = result.items if isinstance(result, CourseEdgeList) else []
                atomic_write_json(
                    part_path,
                    {"items": [edge.model_dump() for edge in part_edges]},
                )
            batch_edges.extend(part_edges)
        generated.extend(batch_edges)
        atomic_write_json(
            batch_path,
            {"items": [edge.model_dump() for edge in batch_edges]},
        )
    valid = {node.name for node in nodes}
    return [
        edge
        for edge in generated
        if edge.source in valid and edge.target in valid and edge.source != edge.target
    ]


def _merge_edges(edges: list[CourseEdge]) -> list[CourseEdge]:
    result: dict[tuple[str, str, str], CourseEdge] = {}
    for edge in edges:
        source, target = edge.source, edge.target
        if edge.edge_type in {"related", "confusable"} and source > target:
            source, target = target, source
        key = (source, target, edge.edge_type)
        normalized = edge.model_copy(
            update={"source": source, "target": target, "status": "pending"}
        )
        if key not in result or normalized.confidence > result[key].confidence:
            result[key] = normalized
    return list(result.values())


def _apply_profile_quality_gates(
    profile: CourseExtractionProfile,
    outline: DocumentOutline,
    nodes: list[CourseNode],
    edges: list[CourseEdge],
) -> tuple[list[CourseNode], list[CourseEdge], list[dict[str, Any]]]:
    """Apply deterministic post-processing rules after model extraction."""
    rules = profile.quality_rules
    outline_ids = {item.id for item in outline.nodes}
    outline_by_name: dict[str, list[str]] = {}
    for item in outline.nodes:
        if item.level == 0:
            continue
        outline_by_name.setdefault(_outline_title_key(item.title), []).append(item.id)
    accepted_nodes: list[CourseNode] = []
    rejected: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for node in nodes:
        exact_outline_matches = outline_by_name.get(normalize_name(node.name), [])
        if len(exact_outline_matches) == 1:
            matched_outline = exact_outline_matches[0]
            node = node.model_copy(
                update={
                    "parent_outline_id": matched_outline,
                    "appearances": sorted(set(node.appearances + [matched_outline])),
                }
            )
        reason: str | None = None
        if (
            rules.require_evidence
            and len(node.evidence.strip()) < rules.minimum_evidence_characters
        ):
            reason = "missing_evidence"
        elif rules.require_parent_outline and not node.parent_outline_id:
            reason = "missing_parent_outline"
        elif rules.reject_unknown_outline and node.parent_outline_id not in outline_ids:
            reason = "unknown_outline"
        elif len(node.name.strip()) > rules.maximum_name_characters:
            reason = "name_too_long"
        elif not node.summary.strip():
            reason = "missing_summary"
        elif normalize_name(node.name) in seen_names:
            reason = "duplicate_node"
        if reason:
            rejected.append({"kind": "node", "name": node.name, "reason": reason})
            continue
        seen_names.add(normalize_name(node.name))
        accepted_nodes.append(node)

    valid_names = {node.name for node in accepted_nodes}
    accepted_edges: dict[tuple[str, str, str], CourseEdge] = {}
    for edge in edges:
        reason = None
        if rules.reject_unknown_endpoints and (
            edge.source not in valid_names or edge.target not in valid_names
        ):
            reason = "unknown_endpoint"
        elif rules.reject_self_loops and edge.source == edge.target:
            reason = "self_loop"
        elif rules.reject_relation_without_evidence and not edge.description.strip():
            reason = "missing_relation_evidence"
        if reason:
            rejected.append(
                {
                    "kind": "edge",
                    "source": edge.source,
                    "target": edge.target,
                    "edge_type": edge.edge_type,
                    "reason": reason,
                }
            )
            continue
        source, target = edge.source, edge.target
        if (
            rules.canonicalize_undirected_edges
            and edge.edge_type in {"related", "confusable"}
            and source > target
        ):
            source, target = target, source
        normalized = edge.model_copy(update={"source": source, "target": target})
        key = (source, target, edge.edge_type)
        previous = accepted_edges.get(key)
        if previous is not None:
            if normalized.confidence > previous.confidence:
                accepted_edges[key] = normalized
            rejected.append(
                {
                    "kind": "edge",
                    "source": source,
                    "target": target,
                    "edge_type": edge.edge_type,
                    "reason": "duplicate_edge",
                }
            )
            continue
        accepted_edges[key] = normalized

    # A specific teaching relation supersedes a broad related edge for the same pair.
    specific_pairs = {
        frozenset((edge.source, edge.target))
        for edge in accepted_edges.values()
        if edge.edge_type != "related"
    }
    filtered_edges: list[CourseEdge] = []
    for key, edge in accepted_edges.items():
        if (
            edge.edge_type == "related"
            and frozenset((edge.source, edge.target)) in specific_pairs
        ):
            rejected.append(
                {
                    "kind": "edge",
                    "source": edge.source,
                    "target": edge.target,
                    "edge_type": edge.edge_type,
                    "reason": "weaker_relation_conflict",
                }
            )
            continue
        filtered_edges.append(edge)
    return accepted_nodes, filtered_edges, rejected


def _quality_report(
    outline: DocumentOutline,
    nodes: list[CourseNode],
    edges: list[CourseEdge],
    expected_outline_ids: set[str] | None = None,
) -> dict[str, Any]:
    substantive = {node.id for node in outline.nodes if node.level > 0}
    expected = (expected_outline_ids or substantive) & substantive
    directly_covered = {appearance for node in nodes for appearance in node.appearances}
    parent_by_id = {node.id: node.parent_id for node in outline.nodes}
    hierarchically_covered = set(directly_covered)
    for outline_id in directly_covered:
        parent_id = parent_by_id.get(outline_id)
        while parent_id is not None:
            hierarchically_covered.add(parent_id)
            parent_id = parent_by_id.get(parent_id)
    uncovered = sorted(expected - hierarchically_covered)
    valid_names = {node.name for node in nodes}
    dangling = [
        edge.model_dump()
        for edge in edges
        if edge.source not in valid_names or edge.target not in valid_names
    ]
    return {
        "outline_sections": len(substantive),
        "extractable_sections": len(expected),
        "covered_sections": len(expected & hierarchically_covered),
        "directly_covered_sections": len(expected & directly_covered),
        "hierarchically_covered_sections": len(expected & hierarchically_covered),
        "outline_coverage": round(
            len(expected & hierarchically_covered) / len(expected), 4
        )
        if expected
        else 1.0,
        "uncovered_section_ids": uncovered,
        "knowledge_points": len(nodes),
        "relations": len(edges),
        "relation_distribution": {
            edge_type: sum(1 for edge in edges if edge.edge_type == edge_type)
            for edge_type in ("prerequisite", "related", "derivative", "confusable")
        },
        "dangling_edges": dangling,
        "passed": not dangling and bool(nodes),
    }


def _write_final_artifacts(
    output: Path,
    outline: DocumentOutline,
    nodes: list[CourseNode],
    edges: list[CourseEdge],
    merge_log: list[dict[str, Any]],
    quality: dict[str, Any],
    *,
    run_id: str,
    profile_version: str,
) -> None:
    atomic_write_json(output / "outline.json", outline.model_dump())
    atomic_write_json(output / "merge-log.json", merge_log)
    atomic_write_json(output / "quality-report.json", quality)
    atomic_write_json(
        output / "source-map.json",
        {
            node.id: {"source_refs": node.source_refs, "appearances": node.appearances}
            for node in nodes
        },
    )
    outline_items = [
        {
            "id": item.id,
            "name": item.title,
            "level": "chapter" if item.level == 1 else "section",
            "parent_id": item.parent_id,
            "order": item.order,
            "source_refs": [ref.model_dump() for ref in item.source_refs],
        }
        for item in outline.nodes
        if item.level > 0
    ]
    point_items = [
        {
            **node.model_dump(),
            "parent_id": node.parent_outline_id,
        }
        for node in nodes
    ]
    name_to_id = {node.name: node.id for node in nodes}
    edge_items = [
        {
            **edge.model_dump(),
            "source_id": name_to_id[edge.source],
            "target_id": name_to_id[edge.target],
        }
        for edge in edges
        if edge.source in name_to_id and edge.target in name_to_id
    ]
    atomic_write_json(
        output / "course-graph-legacy.json",
        {
            "outline": outline_items,
            "nodes": point_items,
            "edges": edge_items,
            "quality": quality,
        },
    )
    node_by_name = {node.name: node for node in nodes}
    knowledge_nodes = [
        CourseKnowledgeNodeV1(
            id=node.id,
            name=node.name,
            level=node.level,
            parent_outline_id=node.parent_outline_id,
            summary=node.summary,
            evidence=node.evidence,
            source_refs=[SourceReference(ref=ref) for ref in node.source_refs],
            profile_version=profile_version,
            run_id=run_id,
            knowledge_kind=node.knowledge_kind,
            aliases=node.aliases,
            learning_objective=node.learning_objective,
            confidence=node.confidence,
        )
        for node in nodes
    ]
    semantic_edges = []
    for edge in edges:
        if edge.source not in name_to_id or edge.target not in name_to_id:
            continue
        fallback_refs = sorted(
            set(
                node_by_name[edge.source].source_refs
                + node_by_name[edge.target].source_refs
            )
        )
        semantic_edges.append(
            CourseSemanticEdgeV1(
                source_id=name_to_id[edge.source],
                target_id=name_to_id[edge.target],
                edge_type=edge.edge_type,
                evidence=edge.description,
                source_refs=[
                    SourceReference(ref=ref)
                    for ref in (edge.source_refs or fallback_refs)
                ],
                confidence=edge.confidence,
                status=edge.status,
            )
        )
    course_graph = build_course_graph_v1(
        outline,
        knowledge_nodes,
        semantic_edges,
        run_id=run_id,
        profile_version=profile_version,
    )
    atomic_write_json(
        output / "course-graph.json", course_graph.model_dump(mode="json")
    )


def run_course_document(
    input_path: str | Path,
    output_dir: str | Path,
    ka: CourseKnowledgeGraph,
    *,
    options: PipelineOptions | None = None,
    input_format: str = "auto",
    resume: bool = True,
    force: bool = False,
    control: PipelineControl | None = None,
) -> dict[str, Any]:
    options = options or PipelineOptions()
    source = Path(input_path).resolve()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    resolved_input_format = _resolve_input_format(source, input_format)
    extraction_brief = None
    if resolved_input_format == "document-package":
        source_hash = document_package_fingerprint(source)
        extraction_brief = load_package_extraction_brief(source)
        if extraction_brief is not None:
            apply_brief = getattr(ka, "apply_extraction_brief", None)
            if not callable(apply_brief):
                raise ValueError(
                    "The selected extraction method does not support ExtractionBrief"
                )
            apply_brief(extraction_brief)
    else:
        source_hash = _file_sha256(source)
    llm_client = getattr(ka, "llm_client", None)
    llm_model = getattr(llm_client, "model_name", None) or getattr(
        llm_client, "model", None
    )
    llm_base = getattr(llm_client, "openai_api_base", None) or getattr(
        llm_client, "base_url", None
    )
    embed_model = getattr(ka.embedder, "model", None) or getattr(
        ka.embedder, "_model", None
    )
    profile_name = str(getattr(ka, "profile_name", "course-knowledge-default"))
    profile_version = str(getattr(ka, "profile_version", COURSE_PROFILE_VERSION))
    profile_hash = str(getattr(ka, "profile_hash", "builtin"))
    prompt_hash = str(getattr(ka, "prompt_hash", "builtin"))
    output_schema_fingerprint = fingerprint(
        {
            "node": CourseNode.model_json_schema(),
            "edge": CourseEdge.model_json_schema(),
            "chunk": CourseChunkResult.model_json_schema(),
            "global_edges": CourseEdgeList.model_json_schema(),
            "dedup": DedupDecision.model_json_schema(),
            "community": CommunityReport.model_json_schema(),
        }
    )
    option_config = dict(options.__dict__)
    option_config.pop("recovery", None)
    if options.recovery is not None:
        option_config["recovery"] = options.recovery.model_dump(mode="json")
    config = {
        **option_config,
        "method": "course_knowledge_graph",
        "pipeline_version": 3,
        "input_format": resolved_input_format,
        "context_planner_version": 1,
        "profile_name": profile_name,
        "profile_version": profile_version,
        "profile_hash": profile_hash,
        "method_fingerprint": prompt_hash,
        "llm_model": str(llm_model or "unknown"),
        "llm_base_url": str(llm_base or ""),
        "llm_profile": os.environ.get("HYPER_EXTRACT_LLM_PROFILE", ""),
        "structured_output_mode": (
            getattr(ka, "structured_output_mode", None)
            or os.environ.get("HYPER_EXTRACT_STRUCTURED_OUTPUT_MODE", "auto")
        ),
        "embedder_model": str(embed_model or "unknown"),
        "model_profile_fingerprint": str(getattr(ka, "model_profile_fingerprint", "")),
        "capability_fingerprint": str(getattr(ka, "capability_fingerprint", "")),
        "adapter_name": str(getattr(ka, "adapter_name", "legacy-langchain")),
        "adapter_version": str(getattr(ka, "adapter_version", "1")),
        "normalizer_version": "1",
        "recovery_policy_version": "1",
        "output_schema_fingerprint": output_schema_fingerprint,
    }
    if extraction_brief is not None:
        config.update(
            {
                "extraction_brief_id": extraction_brief.metadata.id,
                "extraction_brief_version": extraction_brief.metadata.version,
                "extraction_brief_hash": extraction_brief.content_hash,
            }
        )
    if resume and not force:
        _migrate_compatible_profile_checkpoint(
            output,
            source_hash=source_hash,
            config=config,
        )
    checkpoint = RunCheckpoint(
        output,
        source_fingerprint=source_hash,
        config=config,
        resume=resume,
        force=force,
        run_id=control.run_id if control else None,
        event_sink=control.event_sink if control else None,
    )
    configure_artifacts = getattr(ka, "configure_model_artifacts", None)
    if callable(configure_artifacts):
        configure_artifacts(checkpoint.root)
    probe_evidence = getattr(ka, "probe_evidence", None)
    if probe_evidence:
        checkpoint.update(probe_evidence=probe_evidence)
    if extraction_brief is not None:
        normalized_brief = extraction_brief.model_dump(mode="json")
        atomic_write_json(
            checkpoint.root / "extraction-brief.normalized.json", normalized_brief
        )
        atomic_write_text(
            checkpoint.root / "extraction-brief.snapshot.yaml",
            yaml.safe_dump(
                normalized_brief,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ),
        )
        for stage, messages in getattr(ka, "prompt_snapshots", {}).items():
            atomic_write_text(
                checkpoint.root / "prompts" / f"{stage}.txt",
                "### SYSTEM\n"
                + messages.get("system", "")
                + "\n\n### USER TEMPLATE\n"
                + messages.get("user", ""),
            )
    usage_tracker = getattr(ka, "usage_tracker", None)
    if usage_tracker is not None:
        usage_tracker.attach(output / "model-usage.json", resume=resume, force=force)
    previous_handlers: dict[int, Any] = {}

    def interrupt(signum: int, _frame: Any) -> None:
        checkpoint.update(status="interrupted")
        checkpoint.emit("run", "interrupted", f"收到信号 {signum}，已保存现场")
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupt)

    started = time.monotonic()
    try:
        _check_cancel(control)
        checkpoint.update(status="running", stage="ingest")
        checkpoint.emit(
            "ingest",
            "started",
            f"读取结构化文档（{resolved_input_format}）：{source.name}",
        )
        if extraction_brief is not None:
            checkpoint.emit(
                "ingest",
                "progress",
                "加载 ExtractionBrief "
                f"{extraction_brief.metadata.id} v{extraction_brief.metadata.version} "
                f"({extraction_brief.content_hash[:12]})",
            )
        if resolved_input_format == "document-package":
            outline, blocks = load_document_package(source)
        else:
            outline, blocks = load_docling_document(source)
        atomic_write_json(checkpoint.root / "outline.json", outline.model_dump())
        checkpoint.emit(
            "ingest",
            "completed",
            f"解析 {len(outline.nodes) - 1} 个章节、{len(blocks)} 个内容块",
        )

        checkpoint.update(stage="chunk_plan")
        _check_cancel(control)
        checkpoint.emit("chunk_plan", "started", "规划章节感知内容块")
        chunks = plan_document_chunks(
            outline,
            blocks,
            target_tokens=options.target_tokens,
            max_tokens=options.max_tokens,
        )
        atomic_write_json(
            checkpoint.root / "chunks.json", [chunk.model_dump() for chunk in chunks]
        )
        checkpoint.emit(
            "chunk_plan", "completed", f"生成 {len(chunks)} 个章节感知处理块"
        )

        checkpoint.update(stage="local_extract")
        checkpoint.emit(
            "local_extract",
            "started",
            f"开始分析 {len(chunks)} 个内容块",
            current=0,
            total=len(chunks),
        )
        graphs: list[dict[str, Any]] = []
        known_terms: list[str] = []
        for batch_start in range(0, len(chunks), max(1, options.max_workers)):
            _check_cancel(control)
            batch = chunks[batch_start : batch_start + max(1, options.max_workers)]
            pending: list[DocumentChunk] = []
            for chunk in batch:
                graph_path = checkpoint.chunk_dir(chunk.id) / "graph.json"
                if resume and not force and checkpoint.chunk_completed(chunk.id):
                    graph = RunCheckpoint.read_json(graph_path)
                    if graph:
                        graphs.append(graph)
                        known_terms.extend(
                            node.get("name", "") for node in graph.get("nodes", [])
                        )
                    checkpoint.emit(
                        "local_extract",
                        "progress",
                        f"恢复已完成块 {chunk.id}",
                        chunk_id=chunk.id,
                        current=chunk.index + 1,
                        total=len(chunks),
                    )
                else:
                    atomic_write_json(
                        checkpoint.chunk_dir(chunk.id) / "input.json",
                        chunk.model_dump(),
                    )
                    atomic_write_json(
                        checkpoint.chunk_dir(chunk.id) / "status.json",
                        {"status": "running"},
                    )
                    pending.append(chunk)
            if pending:
                with ThreadPoolExecutor(
                    max_workers=max(1, options.max_workers)
                ) as executor:
                    futures = {
                        executor.submit(
                            _extract_chunk,
                            ka,
                            outline,
                            chunk,
                            list(known_terms),
                            checkpoint,
                            options,
                        ): chunk
                        for chunk in pending
                    }
                    completed: dict[int, dict[str, Any]] = {}
                    for future in as_completed(futures):
                        chunk = futures[future]
                        try:
                            graph = future.result()
                        except Exception as error:
                            atomic_write_json(
                                checkpoint.chunk_dir(chunk.id) / "status.json",
                                {
                                    "status": "failed",
                                    "error": f"{type(error).__name__}: {error}",
                                },
                            )
                            raise
                        atomic_write_json(
                            checkpoint.chunk_dir(chunk.id) / "graph.json", graph
                        )
                        atomic_write_json(
                            checkpoint.chunk_dir(chunk.id) / "status.json",
                            {
                                "status": "completed",
                                "nodes": len(graph["nodes"]),
                                "edges": len(graph["edges"]),
                            },
                        )
                        completed[chunk.index] = graph
                        checkpoint.emit(
                            "local_extract",
                            "progress",
                            f"{chunk.id}：{len(graph['nodes'])} 节点 / {len(graph['edges'])} 局部关系",
                            chunk_id=chunk.id,
                            current=chunk.index + 1,
                            total=len(chunks),
                        )
                    for index in sorted(completed):
                        graph = completed[index]
                        graphs.append(graph)
                        known_terms.extend(
                            node.get("name", "") for node in graph.get("nodes", [])
                        )
        checkpoint.emit(
            "local_extract",
            "completed",
            f"完成 {len(chunks)} 个内容块的知识抽取",
            current=len(chunks),
            total=len(chunks),
        )

        checkpoint.update(stage="deduplicate")
        _check_cancel(control)
        checkpoint.emit("deduplicate", "started", "开始合并重复知识点和关系")
        dedup_path = checkpoint.root / "stages" / "deduplicate.json"
        cached_dedup = (
            RunCheckpoint.read_json(dedup_path) if resume and not force else None
        )
        if cached_dedup:
            nodes = [
                CourseNode.model_validate(value)
                for value in cached_dedup.get("nodes", [])
            ]
            local_edges = [
                CourseEdge.model_validate(value)
                for value in cached_dedup.get("edges", [])
            ]
            merge_log = cached_dedup.get("merge_log", [])
            embeddings = cached_dedup.get("embeddings") or []
            if nodes and not embeddings:
                embeddings = _embed_documents_with_quarantine(
                    ka.embedder, [f"{node.name}\n{node.summary}" for node in nodes]
                )
            checkpoint.emit("deduplicate", "progress", "恢复全书去重结果")
        else:
            nodes, local_edges, merge_log, embeddings = _deduplicate(
                ka, graphs, checkpoint, options
            )
            atomic_write_json(
                dedup_path,
                {
                    "nodes": [node.model_dump() for node in nodes],
                    "edges": [edge.model_dump() for edge in local_edges],
                    "merge_log": merge_log,
                    "embeddings": embeddings,
                },
            )
        checkpoint.emit(
            "deduplicate",
            "completed",
            f"全书合并后 {len(nodes)} 个知识点，合并 {len(merge_log)} 项",
        )

        checkpoint.update(stage="global_edges")
        _check_cancel(control)
        checkpoint.emit("global_edges", "started", "开始建立跨章节知识关系")
        global_path = checkpoint.root / "stages" / "global_edges.json"
        cached_global = (
            RunCheckpoint.read_json(global_path) if resume and not force else None
        )
        if cached_global:
            global_edges = [
                CourseEdge.model_validate(value)
                for value in cached_global.get("edges", [])
            ]
            checkpoint.emit("global_edges", "progress", "恢复跨章关系结果")
        else:
            global_edges = (
                _generate_global_edges(
                    ka,
                    outline,
                    nodes,
                    embeddings,
                    local_edges,
                    checkpoint,
                    options,
                )
                if len(nodes) > 1
                else []
            )
            atomic_write_json(
                global_path, {"edges": [edge.model_dump() for edge in global_edges]}
            )
        edges = _merge_edges(local_edges + global_edges)
        nodes, edges, quality_rejections = _apply_profile_quality_gates(
            getattr(ka, "profile", load_course_profile()), outline, nodes, edges
        )
        atomic_write_json(
            checkpoint.root / "stages" / "quality-rejections.json",
            {"items": quality_rejections},
        )
        checkpoint.emit("global_edges", "completed", f"得到 {len(edges)} 条去重关系")

        checkpoint.update(stage="quality")
        _check_cancel(control)
        checkpoint.emit("quality", "started", "开始检查知识图谱完整性")
        expected_outline_ids = {
            outline_id
            for chunk in chunks
            for outline_id in (chunk.covered_outline_ids or [chunk.outline_id])
        }
        quality = _quality_report(outline, nodes, edges, expected_outline_ids)
        atomic_write_json(checkpoint.root / "stages" / "quality.json", quality)
        checkpoint.emit(
            "quality", "completed", f"章节覆盖率 {quality['outline_coverage']:.1%}"
        )

        graph = ka.graph_schema(nodes=nodes, edges=edges)
        ka._set_data_state(graph)
        ka.metadata.update(
            {
                "template": "method/course_knowledge_graph",
                "lang": "zh",
                "type": "graph",
                "source": str(source),
                "source_sha256": source_hash,
                "run_id": checkpoint.run_id,
                **(
                    {
                        "extraction_brief_id": extraction_brief.metadata.id,
                        "extraction_brief_version": extraction_brief.metadata.version,
                        "extraction_brief_hash": extraction_brief.content_hash,
                    }
                    if extraction_brief is not None
                    else {}
                ),
            }
        )

        checkpoint.update(stage="communities")
        _check_cancel(control)
        checkpoint.emit("communities", "started", "开始组织知识主题群组")
        community_path = checkpoint.root / "stages" / "communities.json"
        cached_communities = (
            RunCheckpoint.read_json(community_path) if resume and not force else None
        )
        if cached_communities:
            ka.community_hierarchy = cached_communities.get("hierarchy") or {}
            ka.community_reports = {
                key: CommunityReport.model_validate(value)
                for key, value in (cached_communities.get("reports") or {}).items()
            }
            checkpoint.emit("communities", "progress", "恢复知识社区结果")
        elif nodes:
            hierarchy_path = checkpoint.root / "stages" / "community-hierarchy.json"
            cached_hierarchy = RunCheckpoint.read_json(hierarchy_path)
            if cached_hierarchy:
                ka.community_hierarchy = cached_hierarchy.get("hierarchy") or {}
            else:
                detect_communities = getattr(ka, "detect_communities", None)
                if callable(detect_communities):
                    detect_communities()
                atomic_write_json(hierarchy_path, {"hierarchy": ka.community_hierarchy})
            ka.community_reports = {}
            if options.community_reports:
                community_items = list(ka.community_hierarchy.items())
                for index, (community_id, community) in enumerate(
                    community_items, start=1
                ):
                    report_path = (
                        checkpoint.root
                        / "stages"
                        / "community-reports"
                        / f"{community_id}.json"
                    )
                    cached_report = RunCheckpoint.read_json(report_path)
                    if cached_report:
                        report = CommunityReport.model_validate(cached_report)
                        checkpoint.emit(
                            "communities",
                            "progress",
                            f"恢复社区摘要 {index}/{len(community_items)}",
                            current=index,
                            total=len(community_items),
                        )
                    else:
                        report = _retry(
                            lambda community_id=community_id, community=community: (
                                _with_model_request_metadata(
                                    ka,
                                    lambda: ka.summarize_community(
                                        community_id, community
                                    ),
                                    batch_id=community_id,
                                )
                            ),
                            checkpoint=checkpoint,
                            stage="communities",
                            message=f"生成社区摘要 {index}/{len(community_items)}",
                            attempts=options.retry_attempts,
                            recovery=options.recovery,
                            heartbeat_interval=options.heartbeat_interval,
                        )
                        if report is not None:
                            atomic_write_json(report_path, report.model_dump())
                    if report is not None:
                        ka.community_reports[community_id] = report
            atomic_write_json(
                community_path,
                {
                    "hierarchy": ka.community_hierarchy,
                    "reports": {
                        key: value.model_dump()
                        for key, value in ka.community_reports.items()
                    },
                },
            )
        checkpoint.emit(
            "communities", "completed", f"生成 {len(ka.community_hierarchy)} 个知识社区"
        )

        checkpoint.update(stage="finalize")
        _check_cancel(control)
        checkpoint.emit("finalize", "started", "开始整理最终知识图谱")
        if options.build_index:
            ka.build_index()
        ka.dump(output)
        _write_final_artifacts(
            output,
            outline,
            nodes,
            edges,
            merge_log,
            quality,
            run_id=checkpoint.run_id,
            profile_version=profile_version,
        )
        model_usage = usage_tracker.snapshot() if usage_tracker else None
        global_edge_candidates = (
            RunCheckpoint.read_json(
                checkpoint.root / "stages" / "global-edge-candidates.json"
            )
            or {"count": 0}
        ).get("count", 0)
        wall_elapsed_seconds = round(time.monotonic() - started, 2)
        rejection_summary = _run_rejection_summary(checkpoint.root)
        performance_report = build_performance_report(
            model_usage,
            wall_elapsed_seconds=wall_elapsed_seconds,
            chunks=len(chunks),
            max_workers=options.max_workers,
            global_edge_candidates=global_edge_candidates,
            accepted_edges=len(global_edges),
            resumed=resume and not force,
        )
        cost_report = build_cost_report(
            model_usage,
            input_cost_per_million=_optional_non_negative_float(
                "HYPER_EXTRACT_INPUT_COST_PER_MILLION"
            ),
            output_cost_per_million=_optional_non_negative_float(
                "HYPER_EXTRACT_OUTPUT_COST_PER_MILLION"
            ),
            embedding_input_cost_per_million=_optional_non_negative_float(
                "HYPER_EXTRACT_EMBEDDING_INPUT_COST_PER_MILLION"
            ),
            currency=os.environ.get("HYPER_EXTRACT_COST_CURRENCY"),
        )
        atomic_write_json(output / "performance-report.json", performance_report)
        atomic_write_json(output / "cost-report.json", cost_report)
        summary = {
            "run_id": checkpoint.run_id,
            "status": (
                "completed_with_rejections"
                if rejection_summary["quarantined"]
                else "completed"
            ),
            "input": str(source),
            "input_format": resolved_input_format,
            "output": str(output),
            "outline_nodes": len(outline.nodes),
            "chunks": len(chunks),
            "nodes": len(nodes),
            "edges": len(edges),
            "communities": len(ka.community_hierarchy),
            "profile": {
                "name": profile_name,
                "version": profile_version,
                "content_hash": profile_hash,
                "prompt_hash": prompt_hash,
            },
            "extraction_brief": (
                {
                    "id": extraction_brief.metadata.id,
                    "version": extraction_brief.metadata.version,
                    "content_hash": extraction_brief.content_hash,
                }
                if extraction_brief is not None
                else None
            ),
            "quality": quality,
            "model_usage": model_usage,
            "structured_output": rejection_summary,
            "global_edge_candidates": global_edge_candidates,
            "reports": {
                "quality": "quality-report.json",
                "performance": "performance-report.json",
                "cost": "cost-report.json",
            },
            "elapsed_seconds": wall_elapsed_seconds,
        }
        atomic_write_json(output / "run-summary.json", summary)
        checkpoint.update(status=summary["status"], stage="completed", summary=summary)
        checkpoint.emit(
            "finalize",
            "completed",
            f"全书处理完成：{len(nodes)} 节点 / {len(edges)} 关系",
        )
        return summary
    except (KeyboardInterrupt, RunCancelled):
        summary = {
            "run_id": checkpoint.run_id,
            "status": "interrupted",
            "stage": checkpoint.manifest.get("stage"),
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
        atomic_write_json(output / "run-summary.json", summary)
        checkpoint.update(status="interrupted", summary=summary)
        raise
    except Exception as error:
        failed_stage = str(checkpoint.manifest.get("stage") or "run")
        summary = {
            "run_id": checkpoint.run_id,
            "status": "failed",
            "stage": checkpoint.manifest.get("stage"),
            "error": f"{type(error).__name__}: {error}",
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
        atomic_write_json(output / "run-summary.json", summary)
        checkpoint.update(status="failed", summary=summary)
        if failed_stage in {
            "ingest",
            "chunk_plan",
            "local_extract",
            "deduplicate",
            "global_edges",
            "quality",
            "communities",
            "finalize",
        }:
            checkpoint.emit(failed_stage, "failed", "当前处理阶段执行失败")
        checkpoint.emit("run", "failed", summary["error"])
        raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _run_rejection_summary(root: Path) -> dict[str, Any]:
    counts = {"total": 0, "quarantined": 0, "repaired": 0, "failed": 0}
    affected: dict[str, int] = {}
    affected_requests: set[str] = set()
    affected_chunks: set[str] = set()
    affected_batches: set[str] = set()
    unknown_endpoints: set[str] = set()
    connectivity_warnings: set[str] = set()
    rejected_edges: list[tuple[str, str]] = []
    rejection_root = root / "rejections"
    final_items: dict[tuple[Any, ...], dict[str, Any]] = {}
    if rejection_root.exists():
        for path in rejection_root.glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                identity = (
                    (item.get("rejection_id"),)
                    if item.get("rejection_id")
                    else (
                        item.get("request_id"),
                        item.get("stage"),
                        item.get("chunk_id"),
                        item.get("batch_id"),
                        item.get("schema_path"),
                        json.dumps(item.get("raw_item"), sort_keys=True, default=str),
                    )
                )
                final_items[identity] = item
    for item in final_items.values():
        action = str(item.get("action") or "failed")
        counts["total"] += 1
        if action in counts:
            counts[action] += 1
        if action != "quarantined":
            continue
        _add_lineage(affected_requests, item.get("request_id"))
        _add_lineage(affected_chunks, item.get("chunk_id"))
        _add_lineage(affected_batches, item.get("batch_id"))
        raw = item.get("raw_item")
        if isinstance(raw, dict):
            endpoints: dict[str, str] = {}
            for key in ("source", "target"):
                endpoint = raw.get(key)
                if endpoint:
                    value = str(endpoint)
                    endpoints[key] = value
                    affected[value] = affected.get(value, 0) + 1
                elif key in str(item.get("schema_path") or ""):
                    unknown_endpoints.add(f"{item.get('schema_path')}: missing {key}")
            if endpoints.keys() >= {"source", "target"}:
                rejected_edges.append((endpoints["source"], endpoints["target"]))
    structured_quarantined = counts["quarantined"]
    validation_root = root / "validation"
    if validation_root.exists():
        for path in validation_root.glob("*.json"):
            try:
                validation = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            unknown_endpoints.update(validation.get("unknown_endpoints") or [])
            connectivity_warnings.update(validation.get("connectivity_warnings") or [])
    embedding_requests = 0
    embedding_quarantined = 0
    embedding_root = root / "embedding-rejections"
    if embedding_root.exists():
        for path in embedding_root.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            embedding_requests += 1
            quarantined_items = item.get("quarantined_items") or []
            embedding_quarantined += len(quarantined_items)
            if quarantined_items or item.get("validation_warnings"):
                _add_lineage(affected_requests, item.get("request_id"))
    counts["total"] += embedding_quarantined
    counts["quarantined"] += embedding_quarantined
    connectivity = _rejection_connectivity_impact(root.parent, rejected_edges)
    unknown_endpoints.update(connectivity.pop("unresolved_endpoints"))
    if connectivity["newly_isolated_nodes"]:
        connectivity_warnings.add(
            "Quarantined relationships left previously connected knowledge nodes "
            "isolated"
        )
    if connectivity["additional_connected_components"]:
        connectivity_warnings.add(
            "Quarantined relationships increased semantic connected components"
        )
    return {
        **counts,
        "affected_endpoints": affected,
        "unknown_endpoints": sorted(unknown_endpoints),
        "affected_requests": sorted(affected_requests),
        "affected_chunks": sorted(affected_chunks),
        "affected_batches": sorted(affected_batches),
        "connectivity_warnings": sorted(connectivity_warnings),
        "graph_connectivity_incomplete": structured_quarantined > 0,
        **connectivity,
        "embedding_quality_incomplete": embedding_quarantined > 0,
        "embedding_requests_with_warnings": embedding_requests,
        "embedding_quarantined": embedding_quarantined,
    }


def _add_lineage(target: set[str], value: Any) -> None:
    if value is not None and str(value):
        target.add(str(value))


def _rejection_connectivity_impact(
    output: Path, rejected_edges: list[tuple[str, str]]
) -> dict[str, Any]:
    empty = {
        "newly_isolated_nodes": [],
        "semantic_connected_components": 0,
        "additional_connected_components": 0,
        "unresolved_endpoints": set(),
    }
    graph_path = output / "course-graph.json"
    if not graph_path.is_file():
        return empty
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    nodes = graph.get("knowledge_nodes") or []
    node_ids = {str(node.get("id")) for node in nodes if node.get("id")}
    aliases: dict[str, str] = {}
    for node in nodes:
        node_id = node.get("id")
        if not node_id:
            continue
        aliases[str(node_id)] = str(node_id)
        if node.get("name"):
            aliases[str(node["name"])] = str(node_id)
    current = {node_id: set() for node_id in node_ids}
    for edge in graph.get("semantic_edges") or []:
        source = str(edge.get("source_id") or "")
        target = str(edge.get("target_id") or "")
        if source in current and target in current:
            current[source].add(target)
            current[target].add(source)
    hypothetical = {node_id: set(neighbors) for node_id, neighbors in current.items()}
    unresolved: set[str] = set()
    for source_value, target_value in rejected_edges:
        source = aliases.get(source_value)
        target = aliases.get(target_value)
        if source is None:
            unresolved.add(source_value)
        if target is None:
            unresolved.add(target_value)
        if source is not None and target is not None and source != target:
            hypothetical[source].add(target)
            hypothetical[target].add(source)
    current_components = _component_count(current)
    hypothetical_components = _component_count(hypothetical)
    newly_isolated = sorted(
        node_id
        for node_id in node_ids
        if not current[node_id] and bool(hypothetical[node_id])
    )
    return {
        "newly_isolated_nodes": newly_isolated,
        "semantic_connected_components": current_components,
        "additional_connected_components": max(
            0, current_components - hypothetical_components
        ),
        "unresolved_endpoints": unresolved,
    }


def _component_count(adjacency: dict[str, set[str]]) -> int:
    remaining = set(adjacency)
    components = 0
    while remaining:
        components += 1
        pending = [remaining.pop()]
        while pending:
            current = pending.pop()
            unseen = adjacency[current] & remaining
            remaining.difference_update(unseen)
            pending.extend(unseen)
    return components
