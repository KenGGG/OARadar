"""Add the incremental source Markdown export ledger."""

from alembic import op
import sqlalchemy as sa

revision = "0021_markdown_exports"
down_revision = "0020_production_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "markdown_exports" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "markdown_exports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_file_id", sa.Integer(), sa.ForeignKey("files.id", ondelete="SET NULL")),
        sa.Column("content_object_id", sa.Integer(), sa.ForeignKey("content_objects.id", ondelete="SET NULL")),
        sa.Column("parse_artifact_id", sa.Integer(), sa.ForeignKey("parse_artifacts.id", ondelete="SET NULL")),
        sa.Column("source_sha256", sa.String(64), nullable=False), sa.Column("source_relpath", sa.Text(), nullable=False),
        sa.Column("markdown_relpath", sa.Text(), nullable=False), sa.Column("markdown_sha256", sa.String(64)),
        sa.Column("assets_relpath", sa.Text()), sa.Column("parse_engine", sa.String(40), nullable=False),
        sa.Column("parse_engine_version", sa.String(80), nullable=False), sa.Column("parse_config_hash", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(40), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("quality_score", sa.Float()), sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(80)), sa.Column("last_error", sa.Text()),
        sa.Column("generated_at", sa.DateTime(timezone=True)), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("source_file_id", "schema_version", name="uq_markdown_export_source_schema"),
        sa.UniqueConstraint("markdown_relpath", name="uq_markdown_export_relpath"),
        sa.CheckConstraint("markdown_relpath NOT LIKE '/%' AND markdown_relpath NOT LIKE '%..%'", name="ck_markdown_export_relative"),
    )


def downgrade() -> None:
    op.drop_table("markdown_exports")
