"""DoclingDocument JSON reader and section-aware chunk planner."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable

from .models import (
    DocumentChunk,
    DocumentOutline,
    OutlineNode,
    SourceBlock,
    SourceReference,
)


_HEADING_LABELS = {"section_header", "title"}
_SKIP_LABELS = {"page_header", "page_footer"}
_NUMBERED_HEADING = re.compile(r"^\s*(\d+(?:\.\d+){1,5})(?:\s+|[、．.])")
_CHAPTER_MARKER = re.compile(r"^\s*第\s*(?:第\s*)?(\d+)\s*章\s*$")
_FIGURE_HEADING = re.compile(r"^\s*(?:图|表|图表)\s*\d+(?:[-－.]\d+)*")


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha1(value.encode('utf-8')).hexdigest()[:12]}"


def estimate_tokens(text: str) -> int:
    """Conservative tokenizer-independent estimate for mixed Chinese/English text."""
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    non_cjk = max(0, len(text) - cjk)
    return max(1, cjk + math.ceil(non_cjk / 4))


def _reference(item: dict[str, Any], fallback: str) -> str:
    return str(item.get("self_ref") or fallback)


def _source_refs(item: dict[str, Any], fallback: str) -> list[SourceReference]:
    ref = _reference(item, fallback)
    result: list[SourceReference] = []
    for prov in item.get("prov") or []:
        bbox = prov.get("bbox") if isinstance(prov, dict) else None
        result.append(
            SourceReference(
                ref=ref,
                page_no=prov.get("page_no") if isinstance(prov, dict) else None,
                bbox=bbox if isinstance(bbox, dict) else None,
            )
        )
    return result or [SourceReference(ref=ref)]


def _resolve_ref(document: dict[str, Any], ref: str) -> dict[str, Any] | None:
    if not ref.startswith("#/"):
        return None
    value: Any = document
    try:
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            value = value[int(part)] if isinstance(value, list) else value[part]
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _item_refs(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        ref = value.get("$ref") or value.get("ref")
        if isinstance(ref, str):
            yield ref
    elif isinstance(value, list):
        for item in value:
            yield from _item_refs(item)


def _table_text(item: dict[str, Any]) -> str:
    if isinstance(item.get("text"), str) and item["text"].strip():
        return item["text"].strip()
    cells = (item.get("data") or {}).get("table_cells") or []
    rows: dict[int, list[tuple[int, str]]] = {}
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        row = int(cell.get("start_row_offset_idx", 0))
        col = int(cell.get("start_col_offset_idx", 0))
        rows.setdefault(row, []).append((col, str(cell.get("text") or "").strip()))
    if not rows:
        return ""
    rendered = [
        " | ".join(text for _, text in sorted(rows[row])) for row in sorted(rows)
    ]
    return "\n".join(rendered)


def _item_text(document: dict[str, Any], item: dict[str, Any]) -> str:
    label = str(item.get("label") or "")
    if label == "table":
        return _table_text(item)
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    captions: list[str] = []
    for ref in _item_refs(item.get("captions")):
        caption = _resolve_ref(document, ref)
        if caption and isinstance(caption.get("text"), str):
            captions.append(caption["text"].strip())
    return "\n".join(filter(None, captions))


def _heading_level(item: dict[str, Any], default: int) -> int:
    for key in ("level", "heading_level"):
        value = item.get(key)
        if isinstance(value, int):
            return max(1, value)
    return max(1, default)


def _looks_like_heading(text: str) -> bool:
    stripped = text.strip()
    if not stripped or _FIGURE_HEADING.match(stripped):
        return False
    if len(stripped) > 80:
        return False
    if stripped.endswith(("。", "；", ";", "：", ":", "，", ",")):
        return False
    return True


def load_docling_document(
    path: str | Path,
) -> tuple[DocumentOutline, list[SourceBlock]]:
    source = Path(path)
    with source.open("r", encoding="utf-8") as handle:
        document = json.load(handle)

    if not isinstance(document, dict) or "body" not in document:
        raise ValueError("Input is not a DoclingDocument JSON: missing 'body'.")
    try:
        from docling_core.types.doc import DoclingDocument

        DoclingDocument.model_validate(document)
    except ImportError:
        pass
    except Exception as error:
        raise ValueError(f"Invalid DoclingDocument JSON: {error}") from error

    body = document.get("body") or {}
    heading_levels = [
        _heading_level(item, 1)
        for item in document.get("texts") or []
        if isinstance(item, dict)
        and str(item.get("label") or "") in _HEADING_LABELS
        and _looks_like_heading(str(item.get("text") or ""))
    ]
    numbered_heading_count = sum(
        1
        for item in document.get("texts") or []
        if isinstance(item, dict)
        and _NUMBERED_HEADING.match(str(item.get("text") or ""))
    )
    infer_flat_heading_levels = (
        len(heading_levels) >= 10
        and len(set(heading_levels)) == 1
        and numbered_heading_count >= 3
    )
    root_id = "outline-root"
    outline_nodes = [
        OutlineNode(
            id=root_id, title=str(document.get("name") or source.stem), level=0, order=0
        )
    ]
    blocks: list[SourceBlock] = []
    stack: list[OutlineNode] = [outline_nodes[0]]
    visited: set[str] = set()
    order = 0
    current_major: str | None = None

    def promote_previous_chapter(major: str) -> OutlineNode:
        nonlocal stack
        candidate = outline_nodes[-1] if len(outline_nodes) > 1 else None
        if (
            candidate is not None
            and candidate.level > 0
            and not _NUMBERED_HEADING.match(candidate.title)
            and not _CHAPTER_MARKER.match(candidate.title)
            and _looks_like_heading(candidate.title)
        ):
            candidate.level = 1
            candidate.parent_id = root_id
            stack = [outline_nodes[0], candidate]
            return candidate
        synthetic = OutlineNode(
            id=_stable_id("section", f"synthetic-chapter-{major}"),
            title=f"第 {major} 章",
            level=1,
            parent_id=root_id,
            order=max(0, order - 1),
        )
        outline_nodes.append(synthetic)
        stack = [outline_nodes[0], synthetic]
        return synthetic

    def walk(ref: str) -> None:
        nonlocal order, stack, current_major
        if ref in visited:
            return
        visited.add(ref)
        item = _resolve_ref(document, ref)
        if not item:
            return
        label = str(item.get("label") or "")
        if label in _SKIP_LABELS or str(item.get("content_layer") or "") == "furniture":
            return

        if ref.startswith("#/groups/") or label == "group":
            for child_ref in _item_refs(item.get("children")):
                walk(child_ref)
            return

        text = _item_text(document, item)
        if label in _HEADING_LABELS and text and _looks_like_heading(text):
            numbered = _NUMBERED_HEADING.match(text)
            chapter_marker = _CHAPTER_MARKER.match(text)
            if numbered:
                parts = numbered.group(1).split(".")
                major = parts[0]
                if major != current_major:
                    promote_previous_chapter(major)
                    current_major = major
                level = len(parts)
            elif chapter_marker:
                major = chapter_marker.group(1)
                if major != current_major:
                    promote_previous_chapter(major)
                    current_major = major
                for child_ref in _item_refs(item.get("children")):
                    walk(child_ref)
                return
            elif label == "title":
                level = 1
            else:
                reported = _heading_level(item, len(stack))
                if not infer_flat_heading_levels:
                    level = reported
                else:
                    level = 1 if len(stack) == 1 else min(4, stack[-1].level + 1)
            while len(stack) > 1 and stack[-1].level >= level:
                stack.pop()
            parent = stack[-1]
            order += 1
            node = OutlineNode(
                id=_stable_id("section", f"{ref}|{text}|{order}"),
                title=text,
                level=level,
                parent_id=parent.id,
                order=order,
                source_refs=_source_refs(item, ref),
            )
            outline_nodes.append(node)
            stack.append(node)
            for child_ref in _item_refs(item.get("children")):
                walk(child_ref)
            return

        if not text:
            return
        current = stack[-1]
        path_titles = [node.title for node in stack[1:]]
        top_level = next((node for node in stack if node.level == 1), current)
        order += 1
        blocks.append(
            SourceBlock(
                id=_stable_id("block", f"{ref}|{order}"),
                kind=label or "text",
                text=text,
                outline_id=current.id,
                outline_path=path_titles,
                top_level_id=top_level.id,
                source_refs=_source_refs(item, ref),
            )
        )
        for child_ref in _item_refs(item.get("children")):
            walk(child_ref)

    child_refs = list(_item_refs(body.get("children")))
    if not child_refs and isinstance(body.get("self_ref"), str):
        child_refs = [body["self_ref"]]
    for child_ref in child_refs:
        walk(child_ref)

    if not blocks:
        raise ValueError("DoclingDocument contains no readable body blocks.")

    return (
        DocumentOutline(
            document_name=str(document.get("name") or source.stem),
            schema_name=str(document.get("schema_name") or "DoclingDocument"),
            schema_version=str(document.get("version") or ""),
            nodes=outline_nodes,
        ),
        blocks,
    )


def _split_oversized_block(block: SourceBlock, max_tokens: int) -> list[SourceBlock]:
    if estimate_tokens(block.text) <= max_tokens:
        return [block]
    paragraphs = [
        part.strip() for part in re.split(r"\n\s*\n", block.text) if part.strip()
    ]
    if len(paragraphs) == 1:
        paragraphs = [
            part.strip()
            for part in re.split(r"(?<=[。！？.!?])", block.text)
            if part.strip()
        ]
    pieces: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip()
        if current and estimate_tokens(candidate) > max_tokens:
            pieces.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        pieces.append(current)
    normalized: list[str] = []
    for piece in pieces:
        remaining = piece
        while estimate_tokens(remaining) > max_tokens:
            low, high = 1, len(remaining)
            while low < high:
                midpoint = (low + high + 1) // 2
                if estimate_tokens(remaining[:midpoint]) <= max_tokens:
                    low = midpoint
                else:
                    high = midpoint - 1
            split_at = max(1, low)
            normalized.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        if remaining:
            normalized.append(remaining)
    pieces = normalized
    return [
        block.model_copy(update={"id": f"{block.id}-{i:02d}", "text": text})
        for i, text in enumerate(pieces)
    ]


def plan_document_chunks(
    outline: DocumentOutline,
    blocks: list[SourceBlock],
    *,
    target_tokens: int = 4000,
    max_tokens: int = 6000,
) -> list[DocumentChunk]:
    if target_tokens <= 0 or max_tokens < target_tokens:
        raise ValueError("chunk token limits must satisfy 0 < target <= max")
    expanded = [
        piece for block in blocks for piece in _split_oversized_block(block, max_tokens)
    ]
    chunks: list[DocumentChunk] = []
    current: list[SourceBlock] = []
    current_tokens = 0

    def flush() -> None:
        nonlocal current, current_tokens
        if not current:
            return
        index = len(chunks)
        text = "\n\n".join(block.text for block in current)
        refs = [ref for block in current for ref in block.source_refs]
        last = current[-1]
        covered_ids = list(dict.fromkeys(block.outline_id for block in current))
        covered_paths = list(
            dict.fromkeys(tuple(block.outline_path) for block in current)
        )
        chunks.append(
            DocumentChunk(
                id=f"chunk-{index:05d}",
                index=index,
                outline_id=last.outline_id,
                top_level_id=last.top_level_id,
                outline_path=last.outline_path,
                covered_outline_ids=covered_ids,
                covered_outline_paths=[list(path) for path in covered_paths],
                text=text,
                token_count=estimate_tokens(text),
                source_refs=list({ref.ref: ref for ref in refs}.values()),
                block_ids=[block.id for block in current],
            )
        )
        current = []
        current_tokens = 0

    for block in expanded:
        crosses_chapter = bool(
            current and current[-1].top_level_id != block.top_level_id
        )
        candidate_text = "\n\n".join([*(item.text for item in current), block.text])
        exceeds_max = bool(current and estimate_tokens(candidate_text) > max_tokens)
        section_boundary = bool(current and current[-1].outline_id != block.outline_id)
        if (
            crosses_chapter
            or exceeds_max
            or (section_boundary and current_tokens >= target_tokens)
        ):
            flush()
        current.append(block)
        current_tokens = estimate_tokens("\n\n".join(item.text for item in current))
    flush()
    return chunks


def render_chunk_context(
    outline: DocumentOutline,
    chunk: DocumentChunk,
    *,
    known_terms: list[str] | None = None,
    compact_outline: bool = False,
) -> str:
    terms = "、".join((known_terms or [])[-24:]) or "暂无"
    source_pages = sorted(
        {ref.page_no for ref in chunk.source_refs if ref.page_no is not None}
    )
    covered_ids = chunk.covered_outline_ids or [chunk.outline_id]
    covered_paths = chunk.covered_outline_paths or [chunk.outline_path]
    scope = "\n".join(
        f"- [{outline_id}] {' > '.join(path) or '根目录'}"
        for outline_id, path in zip(covered_ids, covered_paths, strict=False)
    )
    if compact_outline:
        node_map = outline.node_map()
        selected = set(covered_ids)
        for outline_id in tuple(selected):
            cursor = node_map.get(outline_id)
            while cursor and cursor.parent_id:
                selected.add(cursor.parent_id)
                cursor = node_map.get(cursor.parent_id)
        current_top_levels = {
            node.id
            for node in outline.nodes
            if node.level == 1 and node.id == chunk.top_level_id
        }
        for node in outline.nodes:
            cursor = node
            while cursor.parent_id and cursor.level > 1:
                parent = node_map.get(cursor.parent_id)
                if parent is None:
                    break
                cursor = parent
            if cursor.id in current_top_levels:
                selected.add(node.id)
        current_ids = set(covered_ids)
        outline_text = "\n".join(
            f"{'  ' * max(0, node.level - 1)}- [{node.id}] {node.title}"
            + (" <CURRENT>" if node.id in current_ids else "")
            for node in sorted(outline.nodes, key=lambda item: item.order)
            if node.level > 0 and (node.id in selected or node.level == 1)
        )
    else:
        outline_text = outline.render(current_ids=set(covered_ids))
    return (
        "# 文档信息\n"
        f"文档：{outline.document_name}\n"
        "当前块覆盖章节（知识点必须选择最贴近的一项作为 parent_outline_id）：\n"
        f"{scope}\n"
        f"来源页码：{source_pages or '未知'}\n\n"
        "# 全局大纲\n"
        f"{outline_text}\n\n"
        "# 已识别标准术语\n"
        f"{terms}\n\n"
        "# 当前正文\n"
        f"{chunk.text}"
    )
