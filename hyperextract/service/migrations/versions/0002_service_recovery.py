"""Persist run attempts, error history, and worker heartbeats.

Revision ID: 0002_service_recovery
Revises: 0001_service_runs
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_service_recovery"
down_revision = "0001_service_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "he_run_attempts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["he_runs.run_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "attempt", name="uq_he_run_attempts_run_attempt"),
    )

    op.create_table(
        "he_run_errors",
        sa.Column("error_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(96), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("message", sa.String(512), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["he_runs.run_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_he_run_errors_run_id", "he_run_errors", ["run_id"])

    op.create_table(
        "he_worker_heartbeats",
        sa.Column("worker_id", sa.String(128), primary_key=True),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_he_worker_heartbeats_last_seen_at",
        "he_worker_heartbeats",
        ["last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_he_worker_heartbeats_last_seen_at", table_name="he_worker_heartbeats"
    )
    op.drop_table("he_worker_heartbeats")
    op.drop_index("ix_he_run_errors_run_id", table_name="he_run_errors")
    op.drop_table("he_run_errors")
    op.drop_table("he_run_attempts")
