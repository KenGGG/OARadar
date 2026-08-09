"""Pending-notification data cleanup support (plan-0807-1 §6).

Adds the minimal de-duplication ledger columns and cleanup bookkeeping to
``item_occurrences`` so that, once a Feishu delivery is confirmed successful, the
business payload (body, opinion text, page snapshots, temporary attachments,
summaries and delivery copies) can be erased while the small ledger needed to
prevent re-notification is retained.
"""
from alembic import op
import sqlalchemy as sa

revision = "0028_pending_cleanup"
down_revision = "0027_manifest_discovery_hash"
branch_labels = None
depends_on = None


def _has_column(table: str, name: str) -> bool:
    return name in {row["name"] for row in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if _has_column("item_occurrences", "cleanup_status"):
        return
    with op.batch_alter_table("item_occurrences") as batch:
        batch.add_column(sa.Column("cleanup_status", sa.String(30), nullable=True))
        batch.add_column(sa.Column("cleanup_requested_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("cleanup_error_code", sa.String(80), nullable=True))
        batch.add_column(sa.Column("cleanup_attempts", sa.Integer, nullable=False, server_default=sa.text("0")))
        batch.add_column(sa.Column("notify_fingerprint", sa.String(64), nullable=True))
        batch.add_column(sa.Column("allow_renotify", sa.Boolean, nullable=False, server_default=sa.text("1")))


def downgrade() -> None:
    if not _has_column("item_occurrences", "cleanup_status"):
        return
    with op.batch_alter_table("item_occurrences") as batch:
        batch.drop_column("allow_renotify")
        batch.drop_column("notify_fingerprint")
        batch.drop_column("cleanup_attempts")
        batch.drop_column("cleanup_error_code")
        batch.drop_column("cleaned_at")
        batch.drop_column("cleanup_requested_at")
        batch.drop_column("cleanup_status")
