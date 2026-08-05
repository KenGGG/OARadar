"""dedicated markdown conversion queue"""
from alembic import op
import sqlalchemy as sa
revision = "0023_markdown_queue"
down_revision = "0022_online_audit"
branch_labels = None
depends_on = None

def upgrade():
    if "markdown_tasks" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table("markdown_queue_control", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("markdown_tasks", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("source_file_id", sa.Integer(), sa.ForeignKey("files.id", ondelete="CASCADE"), nullable=False), sa.Column("schema_version", sa.String(40), nullable=False), sa.Column("status", sa.String(20), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"), sa.Column("lease_owner", sa.String(120)), sa.Column("lease_expires_at", sa.DateTime(timezone=True)), sa.Column("last_error_code", sa.String(80)), sa.Column("elapsed_seconds", sa.Float()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("source_file_id", "schema_version", name="uq_markdown_tasks_source_schema"))
    op.create_table("markdown_task_events", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("task_id", sa.Integer(), sa.ForeignKey("markdown_tasks.id", ondelete="SET NULL")), sa.Column("event_type", sa.String(50), nullable=False), sa.Column("level", sa.String(20), nullable=False), sa.Column("message", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))

def downgrade():
    op.drop_table("markdown_task_events")
    op.drop_table("markdown_tasks")
    op.drop_table("markdown_queue_control")
