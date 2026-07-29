"""add parse_artifacts table for immutable multi-version parse products."""

from alembic import op
import sqlalchemy as sa

revision = "0012_parse_artifacts"
down_revision = "0011_discovery_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name='parse_artifacts'")
    )
    if result.fetchone():
        return  # Already exists

    op.create_table(
        "parse_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parse_job_id", sa.Integer(), sa.ForeignKey("parse_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("engine", sa.String(20), nullable=False),
        sa.Column("engine_version", sa.String(40), nullable=False),
        sa.Column("output_relpath", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("product_sha256", sa.String(64), nullable=True),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("page_map_json", sa.Text(), nullable=True, server_default=""),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("quality_status", sa.String(20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.timestampnow()),
        sa.UniqueConstraint(
            "parse_job_id", "engine", "engine_version", "output_relpath",
            name="uq_parse_artifact_unique",
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("parse_artifacts") as batch:
        batch.drop_constraint("uq_parse_artifact_unique", type_="unique")
    op.drop_table("parse_artifacts")
