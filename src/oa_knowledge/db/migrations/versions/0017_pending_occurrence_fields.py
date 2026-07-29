"""Add explicit pending-list fields to lifecycle occurrences."""

from alembic import op
import sqlalchemy as sa


revision = "0017_pending_occurrence_fields"
down_revision = "0016_lifecycle_summaries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("item_occurrences")}
    additions = (
        ("previous_approver", sa.Text()),
        ("department", sa.Text()),
        ("initiated_at", sa.DateTime(timezone=True)),
        ("received_at", sa.DateTime(timezone=True)),
        ("deadline_text", sa.Text()),
        ("reminder_count", sa.Integer(), "0"),
        ("processing_status", sa.String(40)),
        ("current_node", sa.Text()),
        ("importance", sa.String(30)),
        ("discovery_hash", sa.String(64)),
    )
    for addition in additions:
        name, column_type, *default = addition
        if name in columns:
            continue
        op.add_column(
            "item_occurrences",
            sa.Column(name, column_type, nullable=False if default else True, server_default=default[0] if default else None),
        )
    op.create_index("ix_occurrences_affair_id", "item_occurrences", ["affair_id_text"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_occurrences_affair_id", table_name="item_occurrences")
    for name in (
        "discovery_hash", "importance", "current_node", "processing_status", "reminder_count",
        "deadline_text", "received_at", "initiated_at", "department", "previous_approver",
    ):
        op.drop_column("item_occurrences", name)
