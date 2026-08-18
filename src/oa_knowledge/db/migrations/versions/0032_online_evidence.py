"""Persist online/local inventory and byte-content evidence fingerprints."""

from alembic import op
import sqlalchemy as sa

revision = "0032_online_evidence"
down_revision = "0031_data_governance"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("online_audit_items")}


def upgrade() -> None:
    columns = _columns()
    additions = (
        ("online_inventory_sha256", sa.String(64), True, None),
        ("local_inventory_sha256", sa.String(64), True, None),
        ("online_content_sha256", sa.String(64), True, None),
        ("local_content_sha256", sa.String(64), True, None),
        ("depth_limit_reached", sa.Boolean(), False, sa.false()),
    )
    for name, type_, nullable, default in additions:
        if name not in columns:
            op.add_column(
                "online_audit_items",
                sa.Column(name, type_, nullable=nullable, server_default=default),
            )


def downgrade() -> None:
    columns = _columns()
    with op.batch_alter_table("online_audit_items") as batch:
        for name in (
            "depth_limit_reached", "local_content_sha256", "online_content_sha256",
            "local_inventory_sha256", "online_inventory_sha256",
        ):
            if name in columns:
                batch.drop_column(name)
