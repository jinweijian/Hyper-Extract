"""Internal models shared by structured document readers and pipelines."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SourceReference(BaseModel):
    ref: str
    page_no: int | None = None
    bbox: dict[str, Any] | None = None
    source_path: str | None = None
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    content_id: str | None = None


class OutlineNode(BaseModel):
    id: str
    title: str
    level: int = Field(ge=0)
    parent_id: str | None = None
    order: int = 0
    source_refs: list[SourceReference] = Field(default_factory=list)


class SourceBlock(BaseModel):
    id: str
    kind: str
    text: str
    outline_id: str
    outline_path: list[str] = Field(default_factory=list)
    top_level_id: str
    source_refs: list[SourceReference] = Field(default_factory=list)


class DocumentOutline(BaseModel):
    document_name: str
    schema_name: str = "DoclingDocument"
    schema_version: str = ""
    nodes: list[OutlineNode] = Field(default_factory=list)

    def node_map(self) -> dict[str, OutlineNode]:
        return {node.id: node for node in self.nodes}

    def render(
        self,
        current_id: str | None = None,
        current_ids: set[str] | None = None,
    ) -> str:
        selected = set(current_ids or [])
        if current_id:
            selected.add(current_id)
        lines: list[str] = []
        for node in sorted(self.nodes, key=lambda item: item.order):
            if node.level == 0:
                continue
            marker = " <CURRENT>" if node.id in selected else ""
            lines.append(
                f"{'  ' * max(0, node.level - 1)}- [{node.id}] {node.title}{marker}"
            )
        return "\n".join(lines)


class DocumentChunk(BaseModel):
    id: str
    index: int
    outline_id: str
    top_level_id: str
    outline_path: list[str]
    covered_outline_ids: list[str] = Field(default_factory=list)
    covered_outline_paths: list[list[str]] = Field(default_factory=list)
    text: str
    token_count: int
    source_refs: list[SourceReference] = Field(default_factory=list)
    block_ids: list[str] = Field(default_factory=list)
    previous_summary: str = ""


class RunEvent(BaseModel):
    timestamp: str
    run_id: str
    stage: str
    status: Literal[
        "started",
        "progress",
        "retrying",
        "heartbeat",
        "completed",
        "failed",
        "interrupted",
    ]
    message: str
    chunk_id: str | None = None
    current: int | None = None
    total: int | None = None
    attempt: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)
