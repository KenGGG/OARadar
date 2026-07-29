"""Link batch items to archived OA details."""

from alembic import op
import sqlalchemy as sa

revision = "0004_detail_archiving"
down_revision = "0003_batch_discovery_fields"
branch_labels = None
depends_on = None

FIELDS = {"oa_item_id", "detail_url", "archive_manifest_relpath", "archived_at"}


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("batch_items")}
    if FIELDS <= existing:
        return
    with op.batch_alter_table("batch_items") as batch:
        batch.add_column(sa.Column("oa_item_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("detail_url", sa.Text(), nullable=True))
        batch.add_column(sa.Column("archive_manifest_relpath", sa.Text(), nullable=True))
        batch.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key("fk_batch_items_oa_item_id", "oa_items", ["oa_item_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("batch_items") as batch:
        batch.drop_constraint("fk_batch_items_oa_item_id", type_="foreignkey")
        for field in ("archived_at", "archive_manifest_relpath", "detail_url", "oa_item_id"):
            batch.drop_column(field)
