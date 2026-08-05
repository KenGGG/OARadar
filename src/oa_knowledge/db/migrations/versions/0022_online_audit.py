"""Add durable online done-item audit runs, items, and events."""
from alembic import op
import sqlalchemy as sa

revision = "0022_online_audit"
down_revision = "0021_markdown_exports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "online_audit_runs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table("online_audit_runs", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("operation_jobs.id", ondelete="SET NULL"), unique=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("total_items", sa.Integer(), nullable=False, server_default="0"), sa.Column("completed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matched_items", sa.Integer(), nullable=False, server_default="0"), sa.Column("mismatch_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("access_failed_items", sa.Integer(), nullable=False, server_default="0"), sa.Column("pause_requested_at", sa.DateTime(timezone=True)),
        sa.Column("current_oa_item_key", sa.String(180)), sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()))
    op.create_table("online_audit_items", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("run_id", sa.Integer(), sa.ForeignKey("online_audit_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("manifest_item_id", sa.Integer(), sa.ForeignKey("oa_manifest_items.id", ondelete="SET NULL")), sa.Column("oa_item_key", sa.String(180), nullable=False),
        sa.Column("workitem_id_text", sa.String()), sa.Column("title", sa.Text(), nullable=False), sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("recognized_attachments", sa.Integer()), sa.Column("database_attachments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("downloaded_attachments", sa.Integer(), nullable=False, server_default="0"), sa.Column("markdown_attachments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(80)), sa.Column("error_detail", sa.Text()), sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)), sa.Column("elapsed_seconds", sa.Float()), sa.UniqueConstraint("run_id", "oa_item_key", name="uq_online_audit_run_item"))
    op.create_table("online_audit_events", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("run_id", sa.Integer(), sa.ForeignKey("online_audit_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("online_audit_items.id", ondelete="SET NULL")), sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False), sa.Column("level", sa.String(20), nullable=False, server_default="info"),
        sa.Column("message", sa.Text(), nullable=False), sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()), sa.UniqueConstraint("run_id", "sequence", name="uq_online_audit_event_sequence"))


def downgrade() -> None:
    op.drop_table("online_audit_events"); op.drop_table("online_audit_items"); op.drop_table("online_audit_runs")
