"""Persist the row position from the online OA Done list."""

from alembic import op
import sqlalchemy as sa


revision = "0036_manifest_list_ordinal"
down_revision = "0035_markdown_item_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("oa_manifest_items")}
    if "list_ordinal" not in columns:
        with op.batch_alter_table("oa_manifest_items") as batch:
            batch.add_column(sa.Column("list_ordinal", sa.Integer(), nullable=True))

    # Existing rows were inserted in discovery order.  Preserve that order until
    # the next online list sync writes the authoritative OA ordinal.
    rows = bind.execute(sa.text(
        "SELECT id, list_page FROM oa_manifest_items ORDER BY list_page, id"
    )).mappings().all()
    page_positions: dict[int, int] = {}
    for row in rows:
        page = int(row["list_page"])
        page_positions[page] = page_positions.get(page, 0) + 1
        bind.execute(sa.text(
            "UPDATE oa_manifest_items SET list_ordinal = :ordinal "
            "WHERE id = :id AND (list_ordinal IS NULL OR list_ordinal = 0)"
        ), {"id": row["id"], "ordinal": page_positions[page]})


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("oa_manifest_items")}
    if "list_ordinal" in columns:
        with op.batch_alter_table("oa_manifest_items") as batch:
            batch.drop_column("list_ordinal")
