"""Persist explainable per-attachment online audit evidence."""

from alembic import op
import sqlalchemy as sa

revision = "0033_online_attachment_evidence"
down_revision = "0032_online_evidence"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("online_audit_items")
    }


def upgrade() -> None:
    columns = _columns()
    additions = (
        ("online_evidence_json", sa.Text(), False, "[]"),
        ("local_evidence_json", sa.Text(), False, "[]"),
        ("comparison_reason", sa.String(80), True, None),
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
        for name in ("comparison_reason", "local_evidence_json", "online_evidence_json"):
            if name in columns:
                batch.drop_column(name)
