"""Add the four V2 Markdown classification fields to OA items."""

from alembic import op
import sqlalchemy as sa


revision = "0034_v2_markdown_classification"
down_revision = "0033_online_attachment_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("oa_items")}
    additions = (
        ("source_type", sa.String(20)),
        ("internal_category", sa.String(80)),
        ("external_issuer", sa.Text()),
        ("classification_version", sa.String(20)),
    )
    for name, type_ in additions:
        if name not in existing:
            op.add_column("oa_items", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("oa_items")}
    with op.batch_alter_table("oa_items") as batch:
        for name in (
            "classification_version",
            "external_issuer",
            "internal_category",
            "source_type",
        ):
            if name in existing:
                batch.drop_column(name)
