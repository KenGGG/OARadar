"""Add immutable batch planning metadata."""

from alembic import op
import sqlalchemy as sa

revision = "0002_batch_planning"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("collection_batches")}
    if {"plan_hash", "created_at", "frozen_at"} <= existing:
        # The bootstrap revision historically used current metadata. Fresh
        # development databases may therefore already contain these fields.
        return
    with op.batch_alter_table("collection_batches") as batch:
        batch.add_column(sa.Column("plan_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE collection_batches SET plan_hash = printf('%064d', id), created_at = CURRENT_TIMESTAMP WHERE plan_hash IS NULL")
    with op.batch_alter_table("collection_batches") as batch:
        batch.alter_column("plan_hash", nullable=False)
        batch.alter_column("created_at", nullable=False)
        batch.create_unique_constraint("uq_collection_batches_plan_hash", ["plan_hash"])


def downgrade() -> None:
    with op.batch_alter_table("collection_batches") as batch:
        batch.drop_constraint("uq_collection_batches_plan_hash", type_="unique")
        batch.drop_column("frozen_at")
        batch.drop_column("created_at")
        batch.drop_column("plan_hash")
