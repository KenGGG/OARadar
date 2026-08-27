"""Bind each classification run item to its adopted decision version."""

import sqlalchemy as sa
from alembic import op

revision = "0039_classification_run_adopted_decision"
down_revision = "0038_oa_markdown_v1_classification"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("classification_run_items")
    }


def _indexes() -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("classification_run_items")
    }


def upgrade() -> None:
    if "adopted_decision_id" not in _columns():
        with op.batch_alter_table("classification_run_items") as batch:
            batch.add_column(
                sa.Column(
                    "adopted_decision_id",
                    sa.Integer(),
                    sa.ForeignKey(
                        "classification_decisions.id",
                        name="fk_classification_run_item_adopted_decision",
                        ondelete="SET NULL",
                    ),
                    nullable=True,
                )
            )
    if "ix_classification_run_item_adopted_decision" not in _indexes():
        op.create_index(
            "ix_classification_run_item_adopted_decision",
            "classification_run_items",
            ["adopted_decision_id"],
        )


def downgrade() -> None:
    if "ix_classification_run_item_adopted_decision" in _indexes():
        op.drop_index(
            "ix_classification_run_item_adopted_decision",
            table_name="classification_run_items",
        )
    if "adopted_decision_id" in _columns():
        with op.batch_alter_table("classification_run_items") as batch:
            batch.drop_column("adopted_decision_id")
