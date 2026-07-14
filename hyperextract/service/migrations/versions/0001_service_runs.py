"""Create asynchronous service run state."""

from alembic import op
import sqlalchemy as sa

revision = "0001_service_runs"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "he_runs",
        sa.Column("run_id", sa.String(40), primary_key=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("output_uri", sa.String(2048), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("stage_status", sa.String(24), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("progress_json", sa.JSON(), nullable=False),
        sa.Column("error_summary_json", sa.JSON(), nullable=True),
        sa.Column("resumable", sa.Boolean(), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_count", sa.Integer(), nullable=False),
        sa.Column("resume_from_checkpoint", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_he_runs_idempotency_key"),
    )
    op.create_index("ix_he_runs_status", "he_runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_he_runs_status", table_name="he_runs")
    op.drop_table("he_runs")
