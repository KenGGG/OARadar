"""Add detail-level identity candidates to item occurrences."""

from alembic import op
import sqlalchemy as sa


revision = "0018_occurrence_identity_fields"
down_revision = "0017_pending_occurrence_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("item_occurrences")}
    for name, column_type in (
        ("activity_id_text", sa.String()),
        ("case_id_text", sa.String()),
        ("template_id_text", sa.String()),
        ("identity_observed_at", sa.DateTime(timezone=True)),
    ):
        if name not in columns:
            op.add_column("item_occurrences", sa.Column(name, column_type, nullable=True))
    op.create_index("ix_occurrences_case_id", "item_occurrences", ["case_id_text"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_occurrences_case_id", table_name="item_occurrences")
    for name in ("identity_observed_at", "template_id_text", "case_id_text", "activity_id_text"):
        op.drop_column("item_occurrences", name)
