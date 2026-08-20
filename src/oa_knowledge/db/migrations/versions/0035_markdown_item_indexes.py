"""Record V2 Markdown item indexes in the existing export ledger."""

from alembic import op
import sqlalchemy as sa


revision = "0035_markdown_item_indexes"
down_revision = "0034_v2_markdown_classification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("markdown_exports")}
    foreign_keys = {key["referred_table"] for key in sa.inspect(bind).get_foreign_keys("markdown_exports")}
    needs_item_foreign_key = "oa_item_id" in columns and "oa_items" not in foreign_keys
    if "oa_item_id" not in columns or "document_kind" not in columns or needs_item_foreign_key:
        # SQLite cannot ALTER TABLE to add a foreign-key constraint.  Batch mode
        # recreates the table safely and also repairs a database where the old
        # direct add-column attempt added oa_item_id before failing on the FK.
        with op.batch_alter_table("markdown_exports", recreate="always") as batch:
            if "oa_item_id" not in columns:
                batch.add_column(sa.Column("oa_item_id", sa.Integer(), nullable=True))
                batch.create_foreign_key(
                    "fk_markdown_exports_oa_item_id", "oa_items", ["oa_item_id"], ["id"], ondelete="SET NULL",
                )
            elif needs_item_foreign_key:
                batch.create_foreign_key(
                    "fk_markdown_exports_oa_item_id", "oa_items", ["oa_item_id"], ["id"], ondelete="SET NULL",
                )
            if "document_kind" not in columns:
                batch.add_column(
                    sa.Column("document_kind", sa.String(20), nullable=False, server_default="attachment"),
                )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("markdown_exports")}
    if "uq_markdown_export_item_index_schema" not in indexes:
        op.create_index(
            "uq_markdown_export_item_index_schema",
            "markdown_exports", ["oa_item_id", "schema_version"], unique=True,
            sqlite_where=sa.text("document_kind = 'item_index'"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("markdown_exports")}
    if "uq_markdown_export_item_index_schema" in indexes:
        op.drop_index("uq_markdown_export_item_index_schema", table_name="markdown_exports")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("markdown_exports")}
    with op.batch_alter_table("markdown_exports") as batch:
        if "document_kind" in columns:
            batch.drop_column("document_kind")
        if "oa_item_id" in columns:
            batch.drop_column("oa_item_id")
