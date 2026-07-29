"""Add source attachment, archive package/member, and document relation boundaries."""

from alembic import op
import sqlalchemy as sa


revision = "0019_archive_sources"
down_revision = "0018_occurrence_identity_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    expected = {
        "source_attachments", "archive_packages", "archive_members", "oa_item_document_relations",
    }
    existing = expected & set(sa.inspect(op.get_bind()).get_table_names())
    if existing == expected:
        return
    if existing:
        raise RuntimeError(f"partial 0019 archive-source schema requires manual recovery: {sorted(existing)}")
    op.create_table(
        "source_attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey("item_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_file_id", sa.Integer(), sa.ForeignKey("files.id", ondelete="SET NULL")),
        sa.Column("source_key", sa.String(180), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("is_main_document", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("display_title", sa.Text()),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column("download_status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("content_object_id", sa.Integer(), sa.ForeignKey("content_objects.id", ondelete="SET NULL")),
        sa.Column("error_code", sa.String(80)),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("snapshot_id", "source_key", name="uq_source_attachment_snapshot_key"),
    )
    op.create_table(
        "archive_packages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_attachment_id", sa.Integer(), sa.ForeignKey("source_attachments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_package_id", sa.Integer(), sa.ForeignKey("archive_packages.id", ondelete="SET NULL")),
        sa.Column("package_key", sa.String(180), nullable=False, unique=True),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("archive_format", sa.String(20), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("compressed_size_bytes", sa.Integer()),
        sa.Column("extracted_size_bytes", sa.Integer()),
        sa.Column("member_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tree_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("security_status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
    )
    op.create_index("ix_archive_packages_sha256", "archive_packages", ["sha256"])
    op.create_table(
        "archive_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("archive_package_id", sa.Integer(), sa.ForeignKey("archive_packages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("child_archive_package_id", sa.Integer(), sa.ForeignKey("archive_packages.id", ondelete="SET NULL")),
        sa.Column("member_key", sa.String(180), nullable=False),
        sa.Column("original_path", sa.Text(), nullable=False),
        sa.Column("normalized_path", sa.Text()),
        sa.Column("member_type", sa.String(30), nullable=False),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("compressed_size_bytes", sa.Integer()),
        sa.Column("sha256", sa.String(64)),
        sa.Column("content_object_id", sa.Integer(), sa.ForeignKey("content_objects.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.String(80)),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("archive_package_id", "member_key", name="uq_archive_member_package_key"),
    )
    op.create_index("ix_archive_members_sha256", "archive_members", ["sha256"])
    op.create_table(
        "oa_item_document_relations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("logical_item_id", sa.Integer(), sa.ForeignKey("logical_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("knowledge_document_id", sa.Integer(), sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_attachment_id", sa.Integer(), sa.ForeignKey("source_attachments.id", ondelete="SET NULL")),
        sa.Column("archive_member_id", sa.Integer(), sa.ForeignKey("archive_members.id", ondelete="SET NULL")),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("is_main_document", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("display_title", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("logical_item_id", "knowledge_document_id", "source_attachment_id", name="uq_oa_document_source"),
    )


def downgrade() -> None:
    op.drop_table("oa_item_document_relations")
    op.drop_index("ix_archive_members_sha256", table_name="archive_members")
    op.drop_table("archive_members")
    op.drop_index("ix_archive_packages_sha256", table_name="archive_packages")
    op.drop_table("archive_packages")
    op.drop_table("source_attachments")
