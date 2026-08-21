"""Add the append-only rebuild output ledger without rebuilding parent tables."""

import sqlalchemy as sa
from alembic import op

revision = "0037_rebuild_outputs"
down_revision = "0036_rebuild_classification_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "rebuild_outputs" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "rebuild_outputs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id", sa.Integer(), sa.ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "oa_item_id", sa.Integer(), sa.ForeignKey("oa_items.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "source_file_id", sa.Integer(), sa.ForeignKey("files.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("target_relpath", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "kind IN ('original', 'parse', 'body_markdown', 'attachment_markdown', 'item_index')",
            name="ck_rebuild_outputs_kind",
        ),
        sa.CheckConstraint("status IN ('success', 'failed')", name="ck_rebuild_outputs_status"),
        sa.UniqueConstraint("run_id", "target_relpath", name="uq_rebuild_outputs_run_target"),
    )
    op.create_index("ix_rebuild_outputs_run_status", "rebuild_outputs", ["run_id", "status"])
    op.create_index("ix_rebuild_outputs_item_kind", "rebuild_outputs", ["oa_item_id", "kind"])


def downgrade() -> None:
    op.drop_index("ix_rebuild_outputs_item_kind", table_name="rebuild_outputs")
    op.drop_index("ix_rebuild_outputs_run_status", table_name="rebuild_outputs")
    op.drop_table("rebuild_outputs")
