"""Add durable production queues, events, and notification delivery state."""

from alembic import op
import sqlalchemy as sa


revision = "0020_production_pipeline"
down_revision = "0019_archive_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "pipeline_runs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table("pipeline_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_key", sa.String(160), unique=True, nullable=False),
        sa.Column("pipeline_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="running"),
        sa.Column("total_tasks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_tasks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_tasks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.create_table("pipeline_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("pipeline_runs.id", ondelete="SET NULL")),
        sa.Column("queue_name", sa.String(40), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("logical_item_key", sa.String(160), nullable=False),
        sa.Column("logical_item_id", sa.Integer(), sa.ForeignKey("logical_items.id", ondelete="SET NULL")),
        sa.Column("stage", sa.String(50), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("idempotency_key", sa.String(220), unique=True, nullable=False),
        sa.Column("progress_current", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("progress_total", sa.Integer()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("error_code", sa.String(80)), sa.Column("last_error", sa.Text()),
        sa.Column("recoverable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)), sa.Column("lease_owner", sa.String(120)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()))
    op.create_index("ix_pipeline_tasks_claim", "pipeline_tasks", ["status", "priority", "created_at"])
    op.create_table("pipeline_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("pipeline_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False), sa.Column("stage", sa.String(50), nullable=False),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()))
    op.create_table("notification_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("logical_item_id", sa.Integer(), sa.ForeignKey("logical_items.id", ondelete="SET NULL")),
        sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey("item_snapshots.id", ondelete="SET NULL")),
        sa.Column("channel", sa.String(30), nullable=False), sa.Column("notification_type", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(220), unique=True, nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message_id", sa.String(180)), sa.Column("error_code", sa.String(80)), sa.Column("last_error", sa.Text()),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)), sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()))


def downgrade() -> None:
    op.drop_table("notification_deliveries")
    op.drop_table("pipeline_events")
    op.drop_index("ix_pipeline_tasks_claim", table_name="pipeline_tasks")
    op.drop_table("pipeline_tasks")
    op.drop_table("pipeline_runs")
