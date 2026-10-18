"""Preserve OA list time text that is not a single complete datetime."""
from alembic import op
import sqlalchemy as sa

revision = "0042_manifest_time_text"
down_revision = "0041_agnes_semantic_decision_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("oa_manifest_items")}
    if "completed_at_text" not in columns:
        op.add_column("oa_manifest_items", sa.Column("completed_at_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("oa_manifest_items", "completed_at_text")
