"""Structured progress snapshots for high-frequency UI updates (Task 6).

The Worker writes a bounded, structured snapshot to
``/exchange/runs/<run_id>/state/progress.json`` instead of updating PostgreSQL
on every pipeline event. The snapshot is atomically replaced via a temp file +
``fsync`` + ``os.replace``. The API reads it back, validates that the
``run_id``/``attempt``/``worker_id`` match the current DB lease, and degrades
safely to a generic message when the file is missing, corrupt, stale, or owned
by a different worker.

Percent is only computed when ``current`` and ``total`` are both valid positive
integers; it is clamped to ``0..100`` and never fabricated.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROGRESS_SCHEMA_VERSION = "1.0"

# Stable activity -> default human-readable message catalogue (Chinese, as the
# service targets the course-graph product). These are the *fallback* messages;
# the pipeline may override ``message`` with a more specific dynamic string.
ACTIVITY_MESSAGES: dict[str, str] = {
    "RUN_QUEUED": "任务已接受，正在等待执行",
    "DOCUMENT_INGESTING": "正在读取文档内容",
    "CHUNK_PLANNING": "正在规划文档处理单元",
    "CONTEXT_PLANNING": "正在规划文档的知识抽取顺序",
    "EXTRACTING_CHUNK": "正在分析内容块",
    "VALIDATING_CHUNK": "正在校验内容块的知识点",
    "MERGING_SECTION": "正在合并章节的知识关系",
    "DEDUPLICATING": "正在消除重复知识点和关系",
    "BUILDING_GLOBAL_EDGES": "正在建立跨章节知识关系",
    "QUALITY_CHECKING": "正在检查知识图谱完整性",
    "BUILDING_COMMUNITIES": "正在组织知识主题群组",
    "FINALIZING": "正在整理最终知识图谱",
    "ARTIFACT_PUBLISHING": "正在发布知识图谱结果",
    "WORKER_RECOVERING": "执行进程已恢复，正在从检查点继续处理",
    "RUN_COMPLETED": "知识图谱抽取完成",
    "RUN_CANCELLED": "任务已取消",
    "RUN_FAILED": "任务执行失败",
}

ACTIVITY_WAITING_MESSAGES: dict[str, tuple[str, ...]] = {
    "DOCUMENT_INGESTING": ("正在读取文档内容", "正在核对文档结构"),
    "CHUNK_PLANNING": ("正在规划文档处理单元", "正在整理章节处理顺序"),
    "CONTEXT_PLANNING": ("正在规划知识抽取顺序", "正在准备领域上下文"),
    "EXTRACTING_CHUNK": (
        "正在分析文档内容",
        "正在识别知识点与证据",
        "正在梳理概念关系",
    ),
    "VALIDATING_CHUNK": ("正在校验知识点", "正在核对知识点的原文证据"),
    "MERGING_SECTION": ("正在合并章节知识", "正在整理章节关系"),
    "DEDUPLICATING": ("正在消除重复知识点", "正在对齐相近概念"),
    "BUILDING_GLOBAL_EDGES": ("正在建立跨章节关系", "正在核对全局知识连接"),
    "QUALITY_CHECKING": ("正在检查图谱完整性", "正在执行质量检查"),
    "BUILDING_COMMUNITIES": ("正在组织知识主题群组", "正在归纳知识主题"),
    "FINALIZING": ("正在整理最终知识图谱", "正在准备结果文件"),
    "ARTIFACT_PUBLISHING": ("正在发布知识图谱结果", "正在校验结果完整性"),
    "WORKER_RECOVERING": ("正在从检查点恢复任务", "正在核对已完成的处理单元"),
}


@dataclass(frozen=True)
class ProgressSnapshot:
    """A bounded, structured progress snapshot written by the Worker."""

    schema_version: str = PROGRESS_SCHEMA_VERSION
    run_id: str = ""
    attempt: int = 1
    worker_id: str = ""
    sequence: int = 0
    stage: str = "queued"
    activity: str = "RUN_QUEUED"
    message: str = ""
    current: int | None = None
    total: int | None = None
    percent: float | None = None
    updated_at: str = field(default_factory=lambda: _now_iso())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_view(self) -> dict[str, Any]:
        """Return the API-safe projection (drops internal worker_id)."""
        return {
            "activity": self.activity,
            "message": self.message or ACTIVITY_MESSAGES.get(self.activity, ""),
            "message_seq": self.sequence,
            "stage": self.stage,
            "progress": {
                "current": self.current,
                "total": self.total,
                "percent": self.percent,
            },
            "updated_at": self.updated_at,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_percent(current: int | None, total: int | None) -> float | None:
    """Compute a clamped percent only when both values are valid positive ints."""
    if current is None or total is None:
        return None
    if total <= 0 or current < 0:
        return None
    pct = (current / total) * 100.0
    if pct < 0:
        return 0.0
    if pct > 100:
        return 100.0
    return round(pct, 2)


def build_snapshot(
    *,
    run_id: str,
    attempt: int,
    worker_id: str,
    sequence: int,
    stage: str,
    activity: str,
    message: str | None = None,
    current: int | None = None,
    total: int | None = None,
) -> ProgressSnapshot:
    return ProgressSnapshot(
        run_id=run_id,
        attempt=attempt,
        worker_id=worker_id,
        sequence=sequence,
        stage=stage,
        activity=activity,
        message=message or ACTIVITY_MESSAGES.get(activity, ""),
        current=current,
        total=total,
        percent=compute_percent(current, total),
        updated_at=_now_iso(),
    )


def rotating_message(activity: str, sequence: int) -> str:
    """Return a safe changing message without fabricating numeric progress."""
    choices = ACTIVITY_WAITING_MESSAGES.get(activity)
    if not choices:
        return ACTIVITY_MESSAGES.get(activity, "任务正在处理中")
    return choices[sequence % len(choices)]


def write_snapshot(progress_path: Path, snapshot: ProgressSnapshot) -> None:
    """Atomically write ``snapshot`` to ``progress_path``.

    Uses a unique temp file in the same directory, ``flush`` + ``fsync``, then
    ``os.replace``. Failures only affect progress display and must NOT bubble
    up to fail the run — callers should catch ``OSError``.
    """
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True)
    directory = progress_path.parent
    fd, tmp_name = tempfile.mkstemp(prefix=".progress-", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, progress_path)
    except OSError:
        # Best-effort cleanup of the temp file on failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_snapshot(progress_path: Path) -> ProgressSnapshot | None:
    """Read and validate a progress snapshot.

    Returns ``None`` when the file is missing, empty, or not valid JSON.
    Returns a best-effort snapshot otherwise — the caller is responsible for
    owner/attempt validation against the DB lease.
    """
    if not progress_path.is_file():
        return None
    try:
        data = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return ProgressSnapshot(
            schema_version=str(data.get("schema_version", PROGRESS_SCHEMA_VERSION)),
            run_id=str(data.get("run_id", "")),
            attempt=int(data.get("attempt", 1)),
            worker_id=str(data.get("worker_id", "")),
            sequence=int(data.get("sequence", 0)),
            stage=str(data.get("stage", "queued")),
            activity=str(data.get("activity", "RUN_QUEUED")),
            message=str(data.get("message", "")),
            current=_safe_int(data.get("current")),
            total=_safe_int(data.get("total")),
            percent=_safe_float(data.get("percent")),
            updated_at=str(data.get("updated_at", _now_iso())),
        )
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_owner_valid(
    snapshot: ProgressSnapshot | None,
    *,
    run_id: str,
    attempt: int,
    lease_owner: str | None,
    max_age_seconds: float | None = None,
) -> bool:
    """Return True iff the snapshot matches the current DB lease owner."""
    if snapshot is None:
        return False
    if not lease_owner:
        return False
    if snapshot.run_id != run_id:
        return False
    if snapshot.attempt != attempt:
        return False
    if snapshot.worker_id != lease_owner:
        return False
    if snapshot.schema_version != PROGRESS_SCHEMA_VERSION:
        return False
    if max_age_seconds is not None:
        try:
            updated_at = datetime.fromisoformat(snapshot.updated_at)
            if updated_at.tzinfo is None:
                return False
            age = (datetime.now(timezone.utc) - updated_at).total_seconds()
        except ValueError:
            return False
        if age < 0 or age > max_age_seconds:
            return False
    return True


# Mapping from pipeline (stage, status) to a stable activity code.
_STAGE_ACTIVITY = {
    "ingest": "DOCUMENT_INGESTING",
    "chunk_plan": "CHUNK_PLANNING",
    "context_planning": "CONTEXT_PLANNING",
    "local_extract": "EXTRACTING_CHUNK",
    "validate": "VALIDATING_CHUNK",
    "merge": "MERGING_SECTION",
    "deduplicate": "DEDUPLICATING",
    "global_edges": "BUILDING_GLOBAL_EDGES",
    "quality": "QUALITY_CHECKING",
    "communities": "BUILDING_COMMUNITIES",
    "finalize": "FINALIZING",
    "publish": "ARTIFACT_PUBLISHING",
    "completed": "RUN_COMPLETED",
    "cancelled": "RUN_CANCELLED",
    "failed": "RUN_FAILED",
    "queued": "RUN_QUEUED",
}


def activity_for_event(stage: str, status: str, *, recovering: bool = False) -> str:
    """Map a pipeline event to a stable activity code."""
    if recovering and status in {"started", "progress"}:
        return "WORKER_RECOVERING"
    activity = _STAGE_ACTIVITY.get(stage)
    if activity is not None:
        return activity
    # Fallback: infer from status.
    if status == "completed":
        return "RUN_COMPLETED"
    if status == "failed":
        return "RUN_FAILED"
    return "EXTRACTING_CHUNK"
