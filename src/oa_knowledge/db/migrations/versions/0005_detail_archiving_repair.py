"""Repair 0004 columns on databases affected by the legacy SQLite bootstrap."""

from alembic import op
import sqlalchemy as sa

revision = "0005_detail_archiving_repair"
down_revision = "0004_detail_archiving"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("batch_items")}
    definitions = {
        "oa_item_id": sa.Column("oa_item_id", sa.Integer(), nullable=True),
        "detail_url": sa.Column("detail_url", sa.Text(), nullable=True),
        "archive_manifest_relpath": sa.Column("archive_manifest_relpath", sa.Text(), nullable=True),
        "archived_at": sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    }
    missing = [name for name in definitions if name not in existing]
    foreign_keys = inspector.get_foreign_keys("batch_items")
    has_item_fk = any(fk.get("referred_table") == "oa_items" and fk.get("constrained_columns") == ["oa_item_id"] for fk in foreign_keys)
    if not missing and has_item_fk:
        return
    with op.batch_alter_table("batch_items") as batch:
        for name in missing:
            batch.add_column(definitions[name])
        if not has_item_fk:
            batch.create_foreign_key("fk_batch_items_oa_item_id", "oa_items", ["oa_item_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    # Repair revisions are intentionally non-destructive.
    pass
