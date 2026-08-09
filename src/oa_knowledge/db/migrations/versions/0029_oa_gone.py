"""Track OA re-sync "gone" state for cleaned pending occurrences.

Adds ``oa_gone_at`` to ``item_occurrences`` so the web console can distinguish a
cleaned occurrence that an on-demand OA re-sync confirmed is no longer present
in OA (title cannot be recovered) from one that is merely awaiting a sync.
"""

from alembic import op
import sqlalchemy as sa

revision = "0029_oa_gone"
down_revision = "0028_pending_cleanup"
branch_labels = None
depends_on = None


def _has_column(table: str, name: str) -> bool:
    return name in {row["name"] for row in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if _has_column("item_occurrences", "oa_gone_at"):
        return
    with op.batch_alter_table("item_occurrences") as batch:
        batch.add_column(sa.Column("oa_gone_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if not _has_column("item_occurrences", "oa_gone_at"):
        return
    with op.batch_alter_table("item_occurrences") as batch:
        batch.drop_column("oa_gone_at")
