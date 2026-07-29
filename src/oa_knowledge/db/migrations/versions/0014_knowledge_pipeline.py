"""Add content-addressed parse and knowledge publication entities."""

from alembic import op
import sqlalchemy as sa


revision = "0014_knowledge_pipeline"
down_revision = "0013_full_manifest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "content_objects" not in tables:
        op.create_table(
        "content_objects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("detected_type", sa.String(40)),
        sa.Column("active_parse_artifact_id", sa.Integer(), sa.ForeignKey("parse_artifacts.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        )
    file_columns = {column["name"] for column in sa.inspect(bind).get_columns("files")}
    if "content_object_id" not in file_columns:
        op.add_column("files", sa.Column("content_object_id", sa.Integer(), nullable=True))
        op.create_index("ix_files_content_object_id", "files", ["content_object_id"])
    artifact_columns = {column["name"] for column in sa.inspect(bind).get_columns("parse_artifacts")}
    if "content_object_id" not in artifact_columns or "lifecycle_status" not in artifact_columns:
        if "content_object_id" not in artifact_columns:
            op.add_column("parse_artifacts", sa.Column("content_object_id", sa.Integer(), nullable=True))
            op.create_index("ix_parse_artifacts_content_object_id", "parse_artifacts", ["content_object_id"])
        if "lifecycle_status" not in artifact_columns:
            op.add_column("parse_artifacts", sa.Column("lifecycle_status", sa.String(20), nullable=False, server_default="valid"))
    tables = set(sa.inspect(bind).get_table_names())
    if "knowledge_documents" not in tables:
        op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("knowledge_key", sa.String(80), nullable=False, unique=True),
        sa.Column("content_object_id", sa.Integer(), sa.ForeignKey("content_objects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("active_parse_artifact_id", sa.Integer(), sa.ForeignKey("parse_artifacts.id", ondelete="SET NULL")),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("vault_relpath", sa.Text()),
        sa.Column("publish_status", sa.String(20), nullable=False, server_default="candidate"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        )
    tables = set(sa.inspect(bind).get_table_names())
    if "source_references" not in tables:
        op.create_table(
        "source_references",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("knowledge_document_id", sa.Integer(), sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_file_id", sa.Integer(), sa.ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("oa_item_id", sa.Integer(), sa.ForeignKey("oa_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("knowledge_document_id", "source_file_id", name="uq_source_reference_document_file"),
        )


def downgrade() -> None:
    op.drop_table("source_references")
    op.drop_table("knowledge_documents")
    op.drop_index("ix_parse_artifacts_content_object_id", table_name="parse_artifacts")
    op.drop_column("parse_artifacts", "lifecycle_status")
    op.drop_column("parse_artifacts", "content_object_id")
    op.drop_index("ix_files_content_object_id", table_name="files")
    op.drop_column("files", "content_object_id")
    op.drop_table("content_objects")
