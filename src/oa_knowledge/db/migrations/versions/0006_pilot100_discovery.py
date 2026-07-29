"""Record cross-page source reconciliation for Pilot-100."""

from alembic import op
import sqlalchemy as sa

revision = "0006_pilot100_discovery"
down_revision = "0005_detail_archiving_repair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    batch_columns = {column["name"] for column in inspector.get_columns("collection_batches")}
    item_columns = {column["name"] for column in inspector.get_columns("batch_items")}
    with op.batch_alter_table("collection_batches") as batch:
        if "source_total_count" not in batch_columns:
            batch.add_column(sa.Column("source_total_count", sa.Integer(), nullable=True))
        if "source_total_pages" not in batch_columns:
            batch.add_column(sa.Column("source_total_pages", sa.Integer(), nullable=True))
        if "pages_scanned" not in batch_columns:
            batch.add_column(sa.Column("pages_scanned", sa.Integer(), nullable=False, server_default="0"))
        if "query_count" not in batch_columns:
            batch.add_column(sa.Column("query_count", sa.Integer(), nullable=False, server_default="0"))
    if "list_page" not in item_columns:
        with op.batch_alter_table("batch_items") as batch:
            batch.add_column(sa.Column("list_page", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("batch_items") as batch:
        batch.drop_column("list_page")
    with op.batch_alter_table("collection_batches") as batch:
        for field in ("query_count", "pages_scanned", "source_total_pages", "source_total_count"):
            batch.drop_column(field)
