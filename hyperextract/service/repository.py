from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .db_models import RunEntity
from .schemas import RunCommand


class IdempotencyConflict(RuntimeError):
    pass


class InvalidRunState(RuntimeError):
    pass


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    status: str
    stage: str
    stage_status: str
    attempt: int
    request_json: dict
    output_uri: str
    progress: dict
    error_summary: dict | None
    resumable: bool
    cancel_requested: bool


def _record(row: RunEntity) -> RunRecord:
    return RunRecord(
        run_id=row.run_id,
        status=row.status,
        stage=row.stage,
        stage_status=row.stage_status,
        attempt=row.attempt,
        request_json=row.request_json,
        output_uri=row.output_uri,
        progress=row.progress_json or {},
        error_summary=row.error_summary_json,
        resumable=row.resumable,
        cancel_requested=row.cancel_requested_at is not None,
    )


class RunRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def create_or_get(self, command: RunCommand, idempotency_key: str):
        with self.session_factory.begin() as session:
            existing = session.scalar(
                select(RunEntity).where(RunEntity.idempotency_key == idempotency_key)
            )
            if existing:
                if existing.request_fingerprint != command.request_fingerprint:
                    raise IdempotencyConflict(idempotency_key)
                return _record(existing), False
            row = RunEntity(
                run_id=command.run_id,
                idempotency_key=idempotency_key,
                request_fingerprint=command.request_fingerprint,
                request_json=command.request_json,
                output_uri=command.output_uri,
            )
            session.add(row)
            session.flush()
            return _record(row), True

    def get(self, run_id: str) -> RunRecord | None:
        with self.session_factory() as session:
            row = session.get(RunEntity, run_id)
            return _record(row) if row else None

    def claim_next(self, worker_id: str, lease_seconds: int = 120):
        with self.session_factory.begin() as session:
            statement = (
                select(RunEntity)
                .where(RunEntity.status == "queued")
                .order_by(RunEntity.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            row = session.scalar(statement)
            if row is None:
                return None
            row.status = "running"
            row.stage_status = "recovering" if row.resume_from_checkpoint else "running"
            row.lease_owner = worker_id
            row.lease_expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=lease_seconds
            )
            return _record(row)

    def update_progress(self, run_id: str, *, stage: str, progress: dict):
        with self.session_factory.begin() as session:
            row = session.get(RunEntity, run_id)
            if row is None:
                raise KeyError(run_id)
            row.stage = stage
            row.stage_status = "running"
            row.progress_json = progress
            return _record(row)

    def request_cancel(self, run_id: str):
        with self.session_factory.begin() as session:
            row = session.get(RunEntity, run_id)
            if row is None:
                raise KeyError(run_id)
            row.cancel_requested_at = datetime.now(timezone.utc)
            if row.status == "queued":
                row.status = "cancelled"
                row.stage_status = "cancelled"
            elif row.status != "running":
                raise InvalidRunState(row.status)
            return _record(row)

    def fail(self, run_id: str, *, code: str, message: str, resumable: bool):
        with self.session_factory.begin() as session:
            row = session.get(RunEntity, run_id)
            if row is None:
                raise KeyError(run_id)
            row.status = "failed"
            row.stage_status = "failed"
            row.error_summary_json = {"code": code, "message": message}
            row.resumable = resumable
            row.lease_owner = None
            row.lease_expires_at = None
            return _record(row)

    def complete(self, run_id: str, summary: dict):
        with self.session_factory.begin() as session:
            row = session.get(RunEntity, run_id)
            if row is None:
                raise KeyError(run_id)
            row.status = "completed"
            row.stage = "completed"
            row.stage_status = "completed"
            row.progress_json = summary
            row.lease_owner = None
            row.lease_expires_at = None
            return _record(row)

    def resume(self, run_id: str):
        with self.session_factory.begin() as session:
            row = session.get(RunEntity, run_id)
            if row is None:
                raise KeyError(run_id)
            if row.status != "failed" or not row.resumable:
                raise InvalidRunState(row.status)
            row.status = "queued"
            row.stage_status = "recovering"
            row.resume_from_checkpoint = True
            row.attempt += 1
            row.error_summary_json = None
            return _record(row)
