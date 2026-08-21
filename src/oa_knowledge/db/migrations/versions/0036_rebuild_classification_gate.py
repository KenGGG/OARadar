"""Persist rebuild classification suggestions, confirmations, and audit events."""

from alembic import op
import sqlalchemy as sa


revision = "0036_rebuild_classification_gate"
down_revision = "0035_markdown_item_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column["name"] for column in inspector.get_columns("oa_items")}
    existing_indexes = {index["name"] for index in inspector.get_indexes("oa_items")}

    # SQLite supports ADD COLUMN with a column-level CHECK constraint.  Adding
    # these fields in place is essential: recreating this referenced parent
    # table while foreign_keys=ON fires CASCADE and SET NULL actions in every
    # dependent table when the old parent is dropped.
    additions = (
        sa.Column("document_date", sa.Date(), nullable=True),
        sa.Column(
            "classification_state",
            sa.String(20),
            sa.CheckConstraint(
                "classification_state IN ('suggested', 'needs_review', 'confirmed')",
                name="ck_oa_items_classification_state",
            ),
            nullable=False,
            server_default=sa.text("'needs_review'"),
        ),
        sa.Column("classification_confidence", sa.Float(), nullable=True),
        sa.Column("classification_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "classification_source",
            sa.String(20),
            sa.CheckConstraint(
                "classification_source IS NULL OR classification_source IN ('rule', 'manual')",
                name="ck_oa_items_classification_source",
            ),
            nullable=True,
        ),
    )
    for column in additions:
        if column.name not in existing_columns:
            op.add_column("oa_items", column)

    if "ix_oa_items_source_channel_classification_state_source_type" not in existing_indexes:
        op.create_index(
            "ix_oa_items_source_channel_classification_state_source_type",
            "oa_items",
            ["source_channel", "classification_state", "source_type"],
        )

    if "rebuild_classification_events" not in inspector.get_table_names():
        op.create_table(
            "rebuild_classification_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("oa_item_id", sa.Integer(), sa.ForeignKey("oa_items.id", ondelete="CASCADE"), nullable=False),
            sa.Column("previous_classification_json", sa.Text(), nullable=False),
            sa.Column("current_classification_json", sa.Text(), nullable=False),
            sa.Column("actor", sa.String(40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )


def downgrade() -> None:
    op.drop_table("rebuild_classification_events")
    op.drop_index("ix_oa_items_source_channel_classification_state_source_type", table_name="oa_items")
    for column_name in (
        "classification_source",
        "classification_confirmed_at",
        "classification_confidence",
        "classification_state",
        "document_date",
    ):
        op.drop_column("oa_items", column_name)
