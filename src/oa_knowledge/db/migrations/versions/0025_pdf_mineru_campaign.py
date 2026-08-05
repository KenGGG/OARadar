"""Add persistent PDF MinerU campaign controls."""
from alembic import op
import sqlalchemy as sa

revision = "0025_pdf_mineru_campaign"
down_revision = "0024_markdown_path_constraint"
branch_labels = None
depends_on = None

def upgrade() -> None:
    if "requested_engine" in {row["name"] for row in sa.inspect(op.get_bind()).get_columns("markdown_tasks")}:
        return
    with op.batch_alter_table("markdown_tasks") as batch:
        batch.add_column(sa.Column("requested_engine", sa.String(30)))
        batch.add_column(sa.Column("campaign", sa.String(40), nullable=False, server_default="standard"))
    with op.batch_alter_table("markdown_queue_control") as batch:
        batch.add_column(sa.Column("pdf_mineru_paused", sa.Boolean(), nullable=False, server_default=sa.false()))

def downgrade() -> None:
    with op.batch_alter_table("markdown_queue_control") as batch: batch.drop_column("pdf_mineru_paused")
    with op.batch_alter_table("markdown_tasks") as batch: batch.drop_column("campaign"); batch.drop_column("requested_engine")
