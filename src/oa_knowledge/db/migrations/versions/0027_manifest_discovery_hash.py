"""Add discovery_hash to oa_manifest_items for idempotent done scans (plan-0806-1 §3.4)."""
from alembic import op
import sqlalchemy as sa

revision = "0027_manifest_discovery_hash"
down_revision = "0026_initiation_archive"
branch_labels = None
depends_on = None


def _has_column(table: str, name: str) -> bool:
    return name in {row["name"] for row in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column("oa_manifest_items", "discovery_hash"):
        with op.batch_alter_table("oa_manifest_items") as batch:
            batch.add_column(sa.Column("discovery_hash", sa.String(), nullable=True))


def downgrade() -> None:
    if _has_column("oa_manifest_items", "discovery_hash"):
        with op.batch_alter_table("oa_manifest_items") as batch:
            batch.drop_column("discovery_hash")
