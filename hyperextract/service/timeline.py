"""Bounded lifecycle timeline for the public HE run status API.

``progress.json`` remains the high-frequency current snapshot.  This module
owns the low-frequency, fixed-size lifecycle summary stored in
``timeline.json``.  It is intentionally independent from the pipeline audit
event log and never exposes model payloads, prompts, filesystem paths, or
tracebacks.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TIMELINE_SCHEMA_VERSION = "1.0"

HE_TIMELINE_ACTIVITIES: tuple[str, ...] = (
    "DOCUMENT_INGESTING",
    "CHUNK_PLANNING",
    "EXTRACTING_CHUNK",
    "DEDUPLICATING",
    "BUILDING_GLOBAL_EDGES",
    "QUALITY_CHECKING",
    "BUILDING_COMMUNITIES",
    "FINALIZING",
    "ARTIFACT_PUBLISHING",
)

TIMELINE_LABELS: dict[str, str] = {
    "DOCUMENT_INGESTING": "读取文档",
    "CHUNK_PLANNING": "规划内容块",
    "EXTRACTING_CHUNK": "抽取知识点",
    "DEDUPLICATING": "合并重复知识点",
    "BUILDING_GLOBAL_EDGES": "建立全局关系",
    "QUALITY_CHECKING": "检查图谱质量",
    "BUILDING_COMMUNITIES": "组织知识主题",
    "FINALIZING": "整理知识图谱",
    "ARTIFACT_PUBLISHING": "发布结果",
}

TIMELINE_STATUSES = frozenset({"pending", "running", "completed", "failed", "skipped"})

PIPELINE_STAGE_ACTIVITY: dict[str, str] = {
    "ingest": "DOCUMENT_INGESTING",
    "chunk_plan": "CHUNK_PLANNING",
    "local_extract": "EXTRACTING_CHUNK",
    "deduplicate": "DEDUPLICATING",
    "global_edges": "BUILDING_GLOBAL_EDGES",
    "quality": "QUALITY_CHECKING",
    "communities": "BUILDING_COMMUNITIES",
    "finalize": "FINALIZING",
    "publish": "ARTIFACT_PUBLISHING",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TimelineProgress:
    current: int | None = None
    total: int | None = None
    percent: float | None = None


@dataclass(frozen=True)
class TimelineStep:
    activity: str
    label: str
    status: str = "pending"
    message: str = ""
    message_seq: int = 0
    progress: TimelineProgress | None = None
    started_at: str | None = None
    completed_at: str | None = None
    attempt: int | None = None


@dataclass(frozen=True)
class TimelineState:
    schema_version: str = TIMELINE_SCHEMA_VERSION
    run_id: str = ""
    worker_id: str = ""
    attempt: int = 1
    sequence: int = 0
    steps: tuple[TimelineStep, ...] = field(default_factory=tuple)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["steps"] = [asdict(step) for step in self.steps]
        return value


def new_timeline(
    run_id: str, *, worker_id: str = "", attempt: int = 1
) -> TimelineState:
    return TimelineState(
        run_id=run_id,
        worker_id=worker_id,
        attempt=attempt,
        steps=tuple(
            TimelineStep(activity=activity, label=TIMELINE_LABELS[activity])
            for activity in HE_TIMELINE_ACTIVITIES
        ),
    )


def prepare_timeline(
    existing: TimelineState | None,
    *,
    run_id: str,
    worker_id: str,
    attempt: int,
) -> TimelineState:
    """Prepare a timeline for a newly claimed attempt.

    Completed/skipped steps are durable across recovery.  A step that was
    running or failed in an older attempt becomes pending until the resumed
    pipeline emits its real lifecycle event.  This avoids fabricating progress
    while preserving monotonic completed history.
    """
    if existing is None or existing.run_id != run_id or existing.attempt > attempt:
        return new_timeline(run_id, worker_id=worker_id, attempt=attempt)
    steps: list[TimelineStep] = []
    for step in existing.steps:
        if step.status in {"completed", "skipped"}:
            steps.append(step)
        else:
            steps.append(
                replace(
                    step,
                    status="pending",
                    message="",
                    message_seq=0,
                    progress=None,
                    # A recovery attempt must not rewrite the first observed
                    # start time of an interrupted stage.
                    started_at=step.started_at,
                    completed_at=None,
                    attempt=None,
                )
            )
    return replace(
        existing,
        worker_id=worker_id,
        attempt=attempt,
        sequence=existing.sequence + 1,
        steps=tuple(steps),
        updated_at=_now_iso(),
    )


def transition(
    state: TimelineState,
    *,
    activity: str | None,
    status: str,
    message: str = "",
    current: int | None = None,
    total: int | None = None,
    timestamp: str | None = None,
) -> TimelineState:
    """Return a new timeline after one low-frequency lifecycle transition."""
    normalized = {
        "started": "running",
        "running": "running",
        "completed": "completed",
        "failed": "failed",
        "skipped": "skipped",
    }.get(status)
    if normalized is None:
        return state

    target_activity = activity
    if target_activity not in HE_TIMELINE_ACTIVITIES:
        if normalized == "failed":
            current_step = next(
                (step for step in state.steps if step.status == "running"), None
            )
            target_activity = (
                current_step.activity
                if current_step is not None
                else next(
                    (step.activity for step in state.steps if step.status == "pending"),
                    None,
                )
            )
        else:
            return state
    if target_activity is None:
        return state

    when = timestamp or _now_iso()
    progress = _progress(current, total)
    target_index = HE_TIMELINE_ACTIVITIES.index(target_activity)
    updated: list[TimelineStep] = []
    for index, step in enumerate(state.steps):
        if step.activity != target_activity:
            # Starting a later real stage is authoritative evidence that all
            # preceding stages have ended, even when a recovered executor did
            # not replay their lifecycle events. Preserve unknown timestamps
            # as null rather than inventing history for pending stages.
            if (
                normalized == "running"
                and index < target_index
                and step.status
                in {
                    "pending",
                    "running",
                }
            ):
                updated.append(
                    replace(
                        step,
                        status="completed",
                        completed_at=(when if step.status == "running" else None),
                        message_seq=step.message_seq + 1,
                        attempt=step.attempt or state.attempt,
                    )
                )
            else:
                updated.append(step)
            continue

        if normalized == "running" and step.status in {"completed", "skipped"}:
            updated.append(step)
            continue
        if normalized == "completed" and step.status == "completed":
            updated.append(step)
            continue

        started_at = step.started_at
        completed_at = step.completed_at
        attempt = step.attempt
        if normalized == "running":
            started_at = started_at or when
            completed_at = None
            attempt = state.attempt
        elif normalized in {"completed", "failed", "skipped"}:
            started_at = started_at or when
            completed_at = when
            attempt = attempt or state.attempt
        updated.append(
            replace(
                step,
                status=normalized,
                message=message or step.message,
                message_seq=step.message_seq + 1,
                progress=progress if progress is not None else step.progress,
                started_at=started_at,
                completed_at=completed_at,
                attempt=attempt,
            )
        )

    return replace(
        state,
        sequence=state.sequence + 1,
        steps=tuple(updated),
        updated_at=when,
    )


def fallback_timeline(
    *,
    run_id: str,
    attempt: int,
    status: str,
    db_stage: str,
    message: str = "",
) -> TimelineState:
    """Build a safe fixed timeline when the lifecycle file is unavailable."""
    state = new_timeline(run_id, attempt=attempt)
    if status == "queued":
        return state
    if status == "completed":
        return replace(
            state,
            steps=tuple(
                replace(step, status="completed", attempt=attempt)
                for step in state.steps
            ),
        )

    activity = PIPELINE_STAGE_ACTIVITY.get(db_stage)
    if activity is None:
        activity = HE_TIMELINE_ACTIVITIES[0]
    target_index = HE_TIMELINE_ACTIVITIES.index(activity)
    target_status = "failed" if status in {"failed", "cancelled"} else "running"
    # A database-only fallback has no authoritative per-stage timestamps.
    # Preserve that fact as null rather than fabricating query-time history.
    return replace(
        state,
        steps=tuple(
            replace(step, status="completed", attempt=attempt)
            if index < target_index
            else replace(
                step,
                status=target_status,
                message=message,
                attempt=attempt,
            )
            if index == target_index
            else step
            for index, step in enumerate(state.steps)
        ),
    )


def current_step(state: TimelineState) -> TimelineStep | None:
    return next(
        (step for step in state.steps if step.status in {"running", "failed"}),
        None,
    )


def ensure_running_activity(
    state: TimelineState, *, activity: str, message: str = ""
) -> TimelineState:
    """Repair a missing lifecycle transition from an owner-valid snapshot.

    The snapshot proves which real pipeline stage the current lease owner is
    executing. Earlier pending steps may therefore be closed without
    inventing timestamps; completed history is preserved.
    """
    if activity not in HE_TIMELINE_ACTIVITIES:
        return state
    target = HE_TIMELINE_ACTIVITIES.index(activity)
    if state.steps[target].status in {"completed", "skipped"}:
        return state
    steps: list[TimelineStep] = []
    for index, step in enumerate(state.steps):
        if index < target and step.status == "pending":
            steps.append(replace(step, status="completed", attempt=state.attempt))
        elif index == target:
            steps.append(
                replace(
                    step,
                    status="running",
                    message=message or step.message,
                    attempt=state.attempt,
                )
            )
        elif step.status == "running":
            steps.append(replace(step, status="pending"))
        else:
            steps.append(step)
    return replace(state, steps=tuple(steps))


def overlay_current(
    state: TimelineState,
    *,
    activity: str,
    message: str,
    message_seq: int,
    current: int | None,
    total: int | None,
) -> TimelineState:
    """Overlay the accepted high-frequency snapshot onto the current step."""
    if activity not in HE_TIMELINE_ACTIVITIES:
        return state
    steps = tuple(
        replace(
            step,
            message=message or step.message,
            message_seq=max(step.message_seq, message_seq),
            progress=_progress(current, total),
        )
        if step.activity == activity and step.status == "running"
        else step
        for step in state.steps
    )
    return replace(state, steps=steps)


def is_valid_for_run(
    state: TimelineState | None,
    *,
    run_id: str,
    attempt: int,
    worker_id: str | None = None,
    require_owner: bool = False,
) -> bool:
    if state is None or state.run_id != run_id or state.attempt != attempt:
        return False
    if require_owner and (not worker_id or state.worker_id != worker_id):
        return False
    return True


def _has_valid_step_order(steps: list[TimelineStep]) -> bool:
    active = [
        index
        for index, step in enumerate(steps)
        if step.status in {"running", "failed"}
    ]
    if len(active) > 1:
        return False
    if active:
        current = active[0]
        return all(
            step.status in {"completed", "skipped"} for step in steps[:current]
        ) and all(
            step.status in {"pending", "skipped"} for step in steps[current + 1 :]
        )
    pending_seen = False
    for step in steps:
        if step.status == "pending":
            pending_seen = True
        elif step.status == "completed" and pending_seen:
            return False
    return True


def write_timeline(path: Path, state: TimelineState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True)
    fd, temporary = tempfile.mkstemp(prefix=".timeline-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_timeline(path: Path) -> TimelineState | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        if value.get("schema_version") != TIMELINE_SCHEMA_VERSION:
            return None
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, list) or len(raw_steps) != len(
            HE_TIMELINE_ACTIVITIES
        ):
            return None
        steps: list[TimelineStep] = []
        for expected, raw in zip(HE_TIMELINE_ACTIVITIES, raw_steps, strict=True):
            if not isinstance(raw, dict) or raw.get("activity") != expected:
                return None
            status = str(raw.get("status", ""))
            if status not in TIMELINE_STATUSES:
                return None
            progress = _parse_progress(raw.get("progress"))
            steps.append(
                TimelineStep(
                    activity=expected,
                    label=str(raw.get("label") or TIMELINE_LABELS[expected]),
                    status=status,
                    message=str(raw.get("message") or ""),
                    message_seq=max(0, int(raw.get("message_seq", 0))),
                    progress=progress,
                    started_at=_optional_string(raw.get("started_at")),
                    completed_at=_optional_string(raw.get("completed_at")),
                    attempt=_positive_int(raw.get("attempt")),
                )
            )
        if not _has_valid_step_order(steps):
            return None
        return TimelineState(
            schema_version=TIMELINE_SCHEMA_VERSION,
            run_id=str(value.get("run_id") or ""),
            worker_id=str(value.get("worker_id") or ""),
            attempt=max(1, int(value.get("attempt", 1))),
            sequence=max(0, int(value.get("sequence", 0))),
            steps=tuple(steps),
            updated_at=str(value.get("updated_at") or ""),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _progress(current: int | None, total: int | None) -> TimelineProgress | None:
    if current is None and total is None:
        return None
    from .progress import compute_percent

    return TimelineProgress(
        current=current,
        total=total,
        percent=compute_percent(current, total),
    )


def _parse_progress(value: Any) -> TimelineProgress | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid progress")
    current = _optional_int(value.get("current"))
    total = _optional_int(value.get("total"))
    percent = value.get("percent")
    return TimelineProgress(
        current=current,
        total=total,
        percent=float(percent) if percent is not None else None,
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    parsed = int(value)
    if parsed < 0:
        raise ValueError("negative value")
    return parsed


def _positive_int(value: Any) -> int | None:
    parsed = _optional_int(value)
    return parsed if parsed and parsed > 0 else None


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None
