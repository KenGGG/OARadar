"""Persist explicit OA evidence that a Done item has no attachments."""

from alembic import op
import sqlalchemy as sa


revision = "0037_no_attachment_evidence"
down_revision = "0036_manifest_list_ordinal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("oa_manifest_items")
    }
    if "no_attachment_confirmed" not in columns:
        with op.batch_alter_table("oa_manifest_items") as batch:
            batch.add_column(sa.Column(
                "no_attachment_confirmed", sa.Boolean(), nullable=False,
                server_default=sa.false(),
            ))


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("oa_manifest_items")
    }
    if "no_attachment_confirmed" in columns:
        with op.batch_alter_table("oa_manifest_items") as batch:
            batch.drop_column("no_attachment_confirmed")
