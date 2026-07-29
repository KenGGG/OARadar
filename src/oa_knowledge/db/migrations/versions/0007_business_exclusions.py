"""Record auditable business exclusions on batch items."""

from alembic import op
import sqlalchemy as sa

revision = "0007_business_exclusions"
down_revision = "0006_pilot100_discovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("batch_items")}
    with op.batch_alter_table("batch_items") as batch:
        if "skip_reason" not in existing:
            batch.add_column(sa.Column("skip_reason", sa.Text(), nullable=True))
        if "policy_version" not in existing:
            batch.add_column(sa.Column("policy_version", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("batch_items") as batch:
        batch.drop_column("policy_version")
        batch.drop_column("skip_reason")
