from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from .commands import RunCommand
from .db_models import (
    RunAttemptEntity,
    RunEntity,
    RunErrorEntity,
    WorkerHeartbeatEntity,
    utcnow,
)


class IdempotencyConflict(RuntimeError):
    pass


class InvalidRunState(RuntimeError):
    pass


class LeaseOwnershipLost(InvalidRunState):
    """Raised when a Worker no longer owns a live lease for a run."""


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
    lease_owner: str | None
    lease_expires_at: datetime | None
    recovery_count: int
    resume_from_checkpoint: bool


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


@dataclass(frozen=True)
class RunAttemptRecord:
    attempt: int
    status: str
    started_at: datetime
    ended_at: datetime | None


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
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        recovery_count=row.recovery_count,
        resume_from_checkpoint=row.resume_from_checkpoint,
    )


def _error_record(row: RunErrorEntity) -> RunErrorRecord:
    return RunErrorRecord(
        attempt=row.attempt,
        code=row.code,
        source=row.source,
        message=row.message,
        occurred_at=row.occurred_at,
    )


def _attempt_record(row: RunAttemptEntity) -> RunAttemptRecord:
    return RunAttemptRecord(
        attempt=row.attempt,
        status=row.status,
        started_at=row.started_at,
        ended_at=row.ended_at,
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

    @staticmethod
    def _set_attempt(
        session,
        row: RunEntity,
        *,
        status: str,
        ended_at: datetime | None = None,
        started_at: datetime | None = None,
    ) -> RunAttemptEntity:
        attempt = session.scalar(
            select(RunAttemptEntity).where(
                RunAttemptEntity.run_id == row.run_id,
                RunAttemptEntity.attempt == row.attempt,
            )
        )
        if attempt is None:
            attempt = RunAttemptEntity(
                run_id=row.run_id,
                attempt=row.attempt,
                status=status,
                started_at=started_at or row.created_at or utcnow(),
                ended_at=ended_at,
            )
            session.add(attempt)
        else:
            attempt.status = status
            attempt.ended_at = ended_at
        return attempt

    @staticmethod
    def _owned_running_row(session, run_id: str, worker_id: str) -> RunEntity:
        now = utcnow()
        row = session.scalar(
            select(RunEntity)
            .where(
                RunEntity.run_id == run_id,
                RunEntity.status == "running",
                RunEntity.lease_owner == worker_id,
                RunEntity.lease_expires_at.isnot(None),
                RunEntity.lease_expires_at >= now,
            )
            .with_for_update()
        )
        if row is None:
            raise LeaseOwnershipLost(run_id)
        return row

    def claim_next(self, worker_id: str, lease_seconds: int = 120):
        with self.session_factory.begin() as session:
            # First, re-claim runs already owned by this worker that have
            # cancellation pending — the worker will finalize them via
            # ``mark_cancelled`` rather than re-executing the pipeline.
            row = session.scalar(
                select(RunEntity)
                .where(
                    RunEntity.status == "running",
                    RunEntity.lease_owner == worker_id,
                    RunEntity.cancel_requested_at.isnot(None),
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is not None:
                row.lease_expires_at = utcnow() + timedelta(seconds=lease_seconds)
                return _record(row)
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
            claimed_at = utcnow()
            row.lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
            self._set_attempt(
                session,
                row,
                status="running",
                started_at=claimed_at,
            )
            return _record(row)

    def update_progress(
        self,
        run_id: str,
        worker_id: str,
        *,
        stage: str,
        progress: dict,
    ):
        with self.session_factory.begin() as session:
            row = self._owned_running_row(session, run_id, worker_id)
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
                self._set_attempt(
                    session,
                    row,
                    status="cancelled",
                    ended_at=utcnow(),
                )
            elif row.status != "running":
                raise InvalidRunState(row.status)
            return _record(row)

    def fail(
        self,
        run_id: str,
        *,
        worker_id: str | None = None,
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
            if worker_id is not None:
                row = self._owned_running_row(session, run_id, worker_id)
            else:
                row = session.get(RunEntity, run_id, with_for_update=True)
            if row is None:
                raise KeyError(run_id)
            if row.status == "running" and worker_id is None:
                raise LeaseOwnershipLost(run_id)
            if row.status in {"completed", "cancelled", "failed"}:
                raise InvalidRunState(row.status)
            attempt_number = row.attempt
            row.status = "failed"
            row.stage_status = "failed"
            row.error_summary_json = {"code": code, "message": message}
            row.resumable = resumable
            row.lease_owner = None
            row.lease_expires_at = None
            self._set_attempt(
                session,
                row,
                status="failed",
                ended_at=occurred_at,
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

    def list_attempts(self, run_id: str) -> list[RunAttemptRecord]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(RunAttemptEntity)
                .where(RunAttemptEntity.run_id == run_id)
                .order_by(RunAttemptEntity.attempt)
            ).all()
            return [_attempt_record(row) for row in rows]

    def complete(
        self,
        run_id: str,
        worker_id: str,
        summary: dict,
        *,
        before_complete: Callable[[], object] | None = None,
    ):
        """Publish and complete while holding the owned run's row lock.

        ``before_complete`` is used for the filesystem publication step. The
        row lock is acquired only after checking the live lease, which gives
        publication and the terminal database transition one linearization
        point relative to lease recovery and claims by another Worker.
        """
        with self.session_factory.begin() as session:
            row = self._owned_running_row(session, run_id, worker_id)
            if before_complete is not None:
                before_complete()
            row.status = "completed"
            row.stage = "completed"
            row.stage_status = "completed"
            row.progress_json = summary
            row.lease_owner = None
            row.lease_expires_at = None
            self._set_attempt(
                session,
                row,
                status="completed",
                ended_at=utcnow(),
            )
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
            # An explicit operator resume starts a fresh recovery window. The
            # previous budget only describes automatic lease recovery for the
            # failed attempt; carrying it forward would make the resumed run
            # fail immediately on its next transient Worker interruption.
            row.recovery_count = 0
            row.attempt += 1
            row.error_summary_json = None
            return _record(row)

    def lease(self, run_id: str) -> RunRecord | None:
        """Return the current run record including lease metadata."""
        return self.get(run_id)

    def mark_cancelled(self, run_id: str, worker_id: str) -> RunRecord:
        """Owner-checked cancellation: verifies ``lease_owner == worker_id``.

        Raises :class:`InvalidRunState` if the run is not ``running`` or is
        owned by a different worker. This is the terminal cancellation
        transition — it is called by the worker after the executor raises
        :class:`RunCancelled`, NOT by ``request_cancel`` (which only sets
        ``cancel_requested_at``).
        """
        with self.session_factory.begin() as session:
            row = self._owned_running_row(session, run_id, worker_id)
            row.status = "cancelled"
            row.stage_status = "cancelled"
            row.lease_owner = None
            row.lease_expires_at = None
            self._set_attempt(
                session,
                row,
                status="cancelled",
                ended_at=utcnow(),
            )
            return _record(row)

    def renew_lease(self, run_id: str, worker_id: str, lease_seconds: int) -> bool:
        """Extend the lease on a running run owned by ``worker_id``.

        Returns ``True`` if the lease was renewed, ``False`` if the run is no
        longer running or owned by a different worker (e.g. the lease expired
        and another worker reclaimed it, or the run completed/cancelled).
        """
        now = utcnow()
        with self.session_factory.begin() as session:
            result = session.execute(
                update(RunEntity)
                .where(
                    RunEntity.run_id == run_id,
                    RunEntity.status == "running",
                    RunEntity.lease_owner == worker_id,
                    RunEntity.lease_expires_at.isnot(None),
                    RunEntity.lease_expires_at >= now,
                )
                .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
            )
            return result.rowcount == 1

    def heartbeat_worker(self, worker_id: str, version: str) -> None:
        """Upsert a worker heartbeat row.

        Called by the worker's daemon heartbeat thread during execution and
        by the main loop when idle. ``version`` is the worker/software
        version string for operator diagnostics.
        """
        with self.session_factory.begin() as session:
            row = session.get(WorkerHeartbeatEntity, worker_id)
            if row is None:
                session.add(
                    WorkerHeartbeatEntity(
                        worker_id=worker_id,
                        version=version,
                        last_seen_at=utcnow(),
                    )
                )
            else:
                row.version = version
                row.last_seen_at = utcnow()

    def requeue_expired_leases(self, max_recoveries: int) -> list[str]:
        """Recover runs whose leases have expired.

        Uses ``SKIP LOCKED`` so multiple workers can call this concurrently.
        For each expired run:

        * If ``cancel_requested_at`` is set → mark ``cancelled`` (the
          cancellation request wins over recovery).
        * If ``recovery_count >= max_recoveries`` → mark ``failed`` with
          ``WORKER_RECOVERY_EXHAUSTED`` and ``resumable=True`` so an operator
          can manually resume.
        * Otherwise → re-queue: ``status = queued``,
          ``stage_status = recovering``, ``recovery_count += 1``,
          ``resume_from_checkpoint = True``. The ``run_id`` is preserved.

        Returns the list of run_ids that were re-queued (not the cancelled
        or failed ones).
        """
        now = utcnow()
        recovered: list[str] = []
        with self.session_factory.begin() as session:
            rows = session.scalars(
                select(RunEntity)
                .where(
                    RunEntity.status == "running",
                    RunEntity.lease_expires_at < now,
                )
                .with_for_update(skip_locked=True)
            )
            for row in rows:
                row.lease_owner = None
                row.lease_expires_at = None
                if row.cancel_requested_at is not None:
                    row.status = "cancelled"
                    row.stage_status = "cancelled"
                    self._set_attempt(
                        session,
                        row,
                        status="cancelled",
                        ended_at=now,
                    )
                elif row.recovery_count >= max_recoveries:
                    row.status = "failed"
                    row.stage_status = "failed"
                    row.resumable = True
                    row.error_summary_json = {
                        "code": "WORKER_RECOVERY_EXHAUSTED",
                        "message": "Worker recovery limit was reached",
                    }
                    self._set_attempt(
                        session,
                        row,
                        status="failed",
                        ended_at=now,
                    )
                    session.add(
                        RunErrorEntity(
                            run_id=row.run_id,
                            attempt=row.attempt,
                            code="WORKER_RECOVERY_EXHAUSTED",
                            source="recovery",
                            message="Worker recovery limit was reached",
                            occurred_at=now,
                        )
                    )
                else:
                    row.status = "queued"
                    row.stage_status = "recovering"
                    row.recovery_count += 1
                    row.resume_from_checkpoint = True
                    self._set_attempt(
                        session,
                        row,
                        status="recovering",
                    )
                    recovered.append(row.run_id)
        return recovered

    def ping(self) -> None:
        """Execute ``SELECT 1`` against the database.

        Raises if the database is unreachable or the connection is broken.
        Used by the readiness endpoint to verify the database is queryable.
        """
        with self.session_factory() as session:
            session.execute(text("SELECT 1"))

    def current_migration_revision(self) -> str | None:
        """Return the Alembic revision recorded in the database, or ``None``.

        Returns ``None`` when the ``alembic_version`` table does not exist
        (migrations have never been applied). This is a readiness failure in
        production but must not crash the health check.
        """
        with self.session_factory() as session:
            engine = session.bind
            if not inspect(engine).has_table("alembic_version"):
                return None
            return session.scalar(text("SELECT version_num FROM alembic_version"))

    def latest_worker_heartbeat(self) -> datetime | None:
        """Return the most recent worker heartbeat timestamp, or ``None``.

        Used by the readiness endpoint to verify at least one Worker has
        reported recently (within ``2 * heartbeat_seconds``).
        """
        with self.session_factory() as session:
            return session.scalar(select(func.max(WorkerHeartbeatEntity.last_seen_at)))

    def delete_worker_heartbeats(self) -> None:
        """Remove every worker heartbeat row.

        Used by readiness tests to simulate a deployment with no recent Worker
        activity. Not called by production code paths.
        """
        with self.session_factory.begin() as session:
            session.execute(delete(WorkerHeartbeatEntity))
