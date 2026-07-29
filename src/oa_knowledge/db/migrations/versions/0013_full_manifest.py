"""Add the canonical full Done-list manifest and sync ledger."""

from alembic import op
import sqlalchemy as sa

revision = "0013_full_manifest"
down_revision = "0012_parse_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if "oa_manifest_items" not in existing:
        op.create_table(
        "oa_manifest_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("oa_item_key", sa.String(), nullable=False, unique=True),
        sa.Column("workitem_id_text", sa.String(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("sender", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("list_page", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processing_status", sa.String(), nullable=False, server_default="discovered"),
        sa.Column("matched_exclusion_keyword", sa.Text(), nullable=True),
        sa.Column("archive_relpath", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("failure_stage", sa.String(), nullable=True),
        sa.Column("last_retry_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "oa_manifest_syncs" not in existing:
        op.create_table(
        "oa_manifest_syncs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("oa_total_count", sa.Integer(), nullable=False),
        sa.Column("local_manifest_count", sa.Integer(), nullable=False),
        sa.Column("pages_scanned", sa.Integer(), nullable=False),
        sa.Column("source_total_pages", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_table("oa_manifest_syncs")
    op.drop_table("oa_manifest_items")
