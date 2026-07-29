"""Add the local Web control-plane job and event tables."""

from alembic import op
import sqlalchemy as sa


revision = "0008_web_control_plane"
down_revision = "0007_business_exclusions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "operation_jobs" not in tables:
        op.create_table(
            "operation_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_key", sa.String(), nullable=False, unique=True),
            sa.Column("job_type", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="queued"),
            sa.Column("idempotency_key", sa.String(), nullable=False, unique=True),
            sa.Column("parameters_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("progress_current", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("progress_total", sa.Integer(), nullable=True),
            sa.Column("lease_owner", sa.String(), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error_code", sa.String(), nullable=True),
            sa.CheckConstraint(
                "status IN ('queued','running','paused','completed','cancelled','auth_required','failed')",
                name="ck_operation_jobs_status",
            ),
        )
    if "operation_events" not in tables:
        op.create_table(
            "operation_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_id", sa.Integer(), sa.ForeignKey("operation_jobs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("job_id", "sequence"),
        )


def downgrade() -> None:
    op.drop_table("operation_events")
    op.drop_table("operation_jobs")
