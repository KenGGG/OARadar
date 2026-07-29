"""Separate raw scanned rows from deduplicated manifest query count."""

from alembic import op
import sqlalchemy as sa

revision = "0011_discovery_reconciliation"
down_revision = "0010_policy_revision_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("collection_batches")}
    if "scanned_row_count" not in columns:
        with op.batch_alter_table("collection_batches") as batch:
            batch.add_column(sa.Column("scanned_row_count", sa.Integer(), nullable=False, server_default="0"))
    connection = op.get_bind()
    connection.execute(sa.text(
        "UPDATE collection_batches "
        "SET scanned_row_count = query_count, query_count = discovered_count "
        "WHERE scanned_row_count = 0 AND query_count > 0"
    ))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text(
        "UPDATE collection_batches SET query_count = scanned_row_count WHERE scanned_row_count > 0"
    ))
    with op.batch_alter_table("collection_batches") as batch:
        batch.drop_column("scanned_row_count")
