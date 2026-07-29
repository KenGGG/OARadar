"""Add frozen list discovery fields to batch items."""

from alembic import op
import sqlalchemy as sa

revision = "0003_batch_discovery_fields"
down_revision = "0002_batch_planning"
branch_labels = None
depends_on = None


FIELDS = {"title", "created_at", "completed_at", "sender", "deadline_text", "category"}


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("batch_items")}
    if FIELDS <= existing:
        return
    with op.batch_alter_table("batch_items") as batch:
        batch.add_column(sa.Column("title", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("sender", sa.Text(), nullable=True))
        batch.add_column(sa.Column("deadline_text", sa.Text(), nullable=True))
        batch.add_column(sa.Column("category", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("batch_items") as batch:
        for field in ("category", "deadline_text", "sender", "completed_at", "created_at", "title"):
            batch.drop_column(field)
