from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .commands import RunCommand
from .db_models import RunAttemptEntity, RunEntity, RunErrorEntity


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


@dataclass(frozen=True)
class RunErrorRecord:
    """Public projection of an error row.

    Deliberately omits ``details_json`` so the repository API can never leak
    sensitive diagnostic payload (exception repr, headers, provider bodies,
    keys, full Prompt content) into responses.
    """

    attempt: int
    code: str
    source: str
    message: str
    occurred_at: datetime


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


def _error_record(row: RunErrorEntity) -> RunErrorRecord:
    return RunErrorRecord(
        attempt=row.attempt,
        code=row.code,
        source=row.source,
        message=row.message,
        occurred_at=row.occurred_at,
    )


class RunRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def create_or_get(self, command: RunCommand, idempotency_key: str):
        """Insert ``command`` or return the existing row for ``idempotency_key``.

        Concurrency contract: the insert is attempted first; if a parallel
        writer wins the race on the unique ``idempotency_key`` constraint, the
        loser catches :class:`IntegrityError`, rolls back, and re-reads the
        winner. This avoids the select-then-insert race that would otherwise
        allow two writers to both believe they should insert.
        """
        new_row = RunEntity(
            run_id=command.run_id,
            idempotency_key=idempotency_key,
            request_fingerprint=command.request_fingerprint,
            request_json=command.request_json,
            output_uri=command.output_uri,
        )
        try:
            with self.session_factory.begin() as session:
                session.add(new_row)
                session.flush()
        except IntegrityError:
            with self.session_factory() as session:
                existing = session.scalar(
                    select(RunEntity).where(
                        RunEntity.idempotency_key == idempotency_key
                    )
                )
                if existing is None:
                    # The IntegrityError was not caused by the unique key we
                    # expected; surface the original failure.
                    raise
                if existing.request_fingerprint != command.request_fingerprint:
                    raise IdempotencyConflict(idempotency_key)
                return _record(existing), False
        return _record(new_row), True

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

    def fail(
        self,
        run_id: str,
        *,
        code: str,
        message: str,
        resumable: bool,
        source: str = "worker",
        details: dict | None = None,
    ):
        """Mark the run failed and persist an error + attempt history row.

        ``details`` is stored in ``he_run_errors.details_json`` for operator
        forensics but is never returned by :meth:`list_errors` — callers must
        never be able to construct a public response from the repository that
        leaks provider bodies, keys, headers, or full Prompt content.
        """
        occurred_at = datetime.now(timezone.utc)
        with self.session_factory.begin() as session:
            row = session.get(RunEntity, run_id)
            if row is None:
                raise KeyError(run_id)
            attempt_number = row.attempt
            row.status = "failed"
            row.stage_status = "failed"
            row.error_summary_json = {"code": code, "message": message}
            row.resumable = resumable
            row.lease_owner = None
            row.lease_expires_at = None
            session.add(
                RunAttemptEntity(
                    run_id=run_id,
                    attempt=attempt_number,
                    status="failed",
                    started_at=row.created_at,
                    ended_at=occurred_at,
                )
            )
            session.add(
                RunErrorEntity(
                    run_id=run_id,
                    attempt=attempt_number,
                    code=code,
                    source=source,
                    message=message,
                    details_json=details,
                    occurred_at=occurred_at,
                )
            )
            return _record(row)

    def list_errors(self, run_id: str) -> list[RunErrorRecord]:
        """Return the public projection of every error recorded for ``run_id``.

        Sensitive ``details_json`` is intentionally dropped at this boundary.
        """
        with self.session_factory() as session:
            rows = session.scalars(
                select(RunErrorEntity)
                .where(RunErrorEntity.run_id == run_id)
                .order_by(RunErrorEntity.error_id)
            ).all()
            return [_error_record(row) for row in rows]

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
