"""Persist the canonical Done-item initiation timestamp."""
from alembic import op
import sqlalchemy as sa

revision = "0026_initiation_archive"
down_revision = "0025_pdf_mineru_campaign"
branch_labels = None
depends_on = None


def _add_if_missing(table: str, name: str) -> None:
    if name not in {row["name"] for row in sa.inspect(op.get_bind()).get_columns(table)}:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column(name, sa.DateTime(timezone=True)))


def upgrade() -> None:
    _add_if_missing("oa_items", "initiated_at")
    _add_if_missing("oa_manifest_items", "initiated_at")


def downgrade() -> None:
    with op.batch_alter_table("oa_manifest_items") as batch:
        batch.drop_column("initiated_at")
    with op.batch_alter_table("oa_items") as batch:
        batch.drop_column("initiated_at")
