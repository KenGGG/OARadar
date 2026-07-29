"""Use SQLite's supported timestamp default for parse artifacts."""

from alembic import op
import sqlalchemy as sa


revision = "0015_parse_artifact_timestamp"
down_revision = "0014_knowledge_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("parse_artifacts") as batch:
        batch.alter_column(
            "created_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        )


def downgrade() -> None:
    with op.batch_alter_table("parse_artifacts") as batch:
        batch.alter_column(
            "created_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=True,
            server_default=None,
        )
