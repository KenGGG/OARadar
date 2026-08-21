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
    existing_constraints = {constraint["name"] for constraint in inspector.get_check_constraints("oa_items")}
    existing_indexes = {index["name"] for index in inspector.get_indexes("oa_items")}
    with op.batch_alter_table("oa_items", recreate="always") as batch:
        additions = (
            ("document_date", sa.Date(), True, None),
            (
                "classification_state",
                sa.String(20),
                False,
                sa.text("'needs_review'"),
            ),
            ("classification_confidence", sa.Float(), True, None),
            ("classification_confirmed_at", sa.DateTime(timezone=True), True, None),
            ("classification_source", sa.String(20), True, None),
        )
        for name, type_, nullable, server_default in additions:
            if name not in existing_columns:
                batch.add_column(sa.Column(name, type_, nullable=nullable, server_default=server_default))
        if "ck_oa_items_classification_state" not in existing_constraints:
            batch.create_check_constraint(
                "ck_oa_items_classification_state",
                "classification_state IN ('suggested', 'needs_review', 'confirmed')",
            )
        if "ck_oa_items_classification_source" not in existing_constraints:
            batch.create_check_constraint(
                "ck_oa_items_classification_source",
                "classification_source IS NULL OR classification_source IN ('rule', 'manual')",
            )
        if "ix_oa_items_source_channel_classification_state_source_type" not in existing_indexes:
            batch.create_index(
                "ix_oa_items_source_channel_classification_state_source_type",
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
    with op.batch_alter_table("oa_items", recreate="always") as batch:
        batch.drop_index("ix_oa_items_source_channel_classification_state_source_type")
        batch.drop_constraint("ck_oa_items_classification_source", type_="check")
        batch.drop_constraint("ck_oa_items_classification_state", type_="check")
        batch.drop_column("classification_source")
        batch.drop_column("classification_confirmed_at")
        batch.drop_column("classification_confidence")
        batch.drop_column("classification_state")
        batch.drop_column("document_date")
