"""Add exclusion_policies table for web-managed attachment policies."""

from alembic import op
import sqlalchemy as sa

revision = "0009_exclusion_policies"
down_revision = "0008_web_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "exclusion_policies" in inspector.get_table_names():
        return
    op.create_table(
        "exclusion_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("action", sa.String(20), nullable=False, server_default="skip"),
        sa.Column("scope", sa.String(20), nullable=False, server_default="title"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("version", sa.String(40), nullable=False, server_default="v1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('skip', 'metadata_only')",
            name="ck_exclusion_policies_action",
        ),
        sa.CheckConstraint(
            "scope IN ('title', 'sender', 'category', 'full')",
            name="ck_exclusion_policies_scope",
        ),
    )


def downgrade() -> None:
    op.drop_table("exclusion_policies")
