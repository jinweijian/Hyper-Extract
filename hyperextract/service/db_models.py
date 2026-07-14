from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunEntity(Base):
    __tablename__ = "he_runs"

    run_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    request_json: Mapped[dict] = mapped_column(JSON)
    output_uri: Mapped[str] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(24), index=True, default="queued")
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    stage_status: Mapped[str] = mapped_column(String(24), default="waiting")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    progress_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resumable: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovery_count: Mapped[int] = mapped_column(Integer, default=0)
    resume_from_checkpoint: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RunAttemptEntity(Base):
    __tablename__ = "he_run_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("he_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Ensures only one row per (run, attempt number) — required for replaying
        # attempt history and for stable recovery semantics.
        UniqueConstraint(
            "run_id", "attempt", name="uq_he_run_attempts_run_attempt"
        ),
    )


class RunErrorEntity(Base):
    __tablename__ = "he_run_errors"

    error_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("he_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String(96), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(String(512), nullable=False)
    details_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class WorkerHeartbeatEntity(Base):
    __tablename__ = "he_worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
