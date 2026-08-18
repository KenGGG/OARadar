"""Add privacy-safe data-governance cleanup ledgers."""

from alembic import op
import sqlalchemy as sa

revision = "0031_data_governance"
down_revision = "0030_curated_knowledge"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "cleanup_runs" not in tables:
        op.create_table(
            "cleanup_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("rules_version", sa.String(40), nullable=False),
            sa.Column("categories_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("candidate_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("quarantined_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("quarantined_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("restored_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("restored_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("purged_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("purged_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_code", sa.String(80)),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
            sa.CheckConstraint(
                "status IN ('planning','planned','quarantining','quarantined',"
                "'restoring','restored','purging','purged','failed')",
                name="ck_cleanup_run_status",
            ),
        )
    tables = _tables()
    if "cleanup_items" not in tables:
        op.create_table(
            "cleanup_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("cleanup_run_id", sa.Integer(), sa.ForeignKey("cleanup_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("relative_path", sa.Text(), nullable=False),
            sa.Column("category", sa.String(40), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("preflight_sha256", sa.String(64)),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("quarantine_relpath", sa.Text()),
            sa.Column("reason_code", sa.String(80), nullable=False),
            sa.Column("error_code", sa.String(80)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("cleanup_run_id", "relative_path", name="uq_cleanup_item_run_path"),
            sa.CheckConstraint(
                "status IN ('planned','quarantined','restored','purged','skipped','failed')",
                name="ck_cleanup_item_status",
            ),
            sa.CheckConstraint("size_bytes >= 0", name="ck_cleanup_item_size"),
            sa.CheckConstraint(
                "relative_path <> '' AND substr(relative_path, 1, 1) <> '/' "
                "AND relative_path <> '..' AND relative_path NOT LIKE '../%' "
                "AND relative_path NOT LIKE '%/../%' AND relative_path NOT LIKE '%/..' "
                "AND relative_path <> '.' AND relative_path NOT LIKE './%' "
                "AND relative_path NOT LIKE '%/./%' AND relative_path NOT LIKE '%/.'",
                name="ck_cleanup_item_relative_path",
            ),
        )


def downgrade() -> None:
    tables = _tables()
    for table in ("cleanup_items", "cleanup_runs"):
        if table in tables:
            op.drop_table(table)
