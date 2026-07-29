"""Add immutable exclusion policy revision audit."""

from datetime import datetime, timezone
import hashlib
import json

from alembic import op
import sqlalchemy as sa

revision = "0010_policy_revision_audit"
down_revision = "0009_exclusion_policies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "exclusion_policy_revisions" not in inspector.get_table_names():
        op.create_table(
            "exclusion_policy_revisions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("policy_id", sa.Integer(), sa.ForeignKey("exclusion_policies.id", ondelete="SET NULL")),
            sa.Column("policy_name", sa.String(120), nullable=False),
            sa.Column("version", sa.String(40), nullable=False),
            sa.Column("change_type", sa.String(20), nullable=False),
            sa.Column("snapshot_json", sa.Text(), nullable=False),
            sa.Column("snapshot_sha256", sa.String(64), nullable=False),
            sa.Column("actor", sa.String(40), nullable=False, server_default="migration"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("change_type IN ('created','updated','deleted','backfilled')", name="ck_policy_revision_change_type"),
        )
    policies = sa.table(
        "exclusion_policies",
        sa.column("id"), sa.column("name"), sa.column("description"), sa.column("pattern"),
        sa.column("action"), sa.column("scope"), sa.column("enabled"), sa.column("version"),
    )
    revisions = sa.table(
        "exclusion_policy_revisions",
        sa.column("policy_id"), sa.column("policy_name"), sa.column("version"),
        sa.column("change_type"), sa.column("snapshot_json"), sa.column("snapshot_sha256"),
        sa.column("actor"), sa.column("created_at"),
    )
    connection = op.get_bind()
    if connection.scalar(sa.select(sa.func.count()).select_from(revisions)) == 0:
        for row in connection.execute(sa.select(policies)).mappings():
            snapshot = json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute(revisions.insert().values(
                policy_id=row["id"], policy_name=row["name"], version=row["version"],
                change_type="backfilled", snapshot_json=snapshot,
                snapshot_sha256=hashlib.sha256(snapshot.encode()).hexdigest(), actor="migration",
                created_at=datetime.now(timezone.utc),
            ))


def downgrade() -> None:
    op.drop_table("exclusion_policy_revisions")
