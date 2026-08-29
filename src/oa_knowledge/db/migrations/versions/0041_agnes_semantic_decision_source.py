"""Permit audited Agnes semantic decisions without changing OA package schema."""

from alembic import op

revision = "0041_agnes_semantic_decision_source"
down_revision = "0040_external_review_without_issuer"
branch_labels = None
depends_on = None

_OLD = "decision_source IN ('metadata_rule', 'content_rule', 'local_qwen', 'manual')"
_NEW = "decision_source IN ('metadata_rule', 'content_rule', 'local_qwen', 'agnes', 'manual')"


def upgrade() -> None:
    with op.batch_alter_table("classification_decisions") as batch:
        batch.drop_constraint("ck_classification_decision_source", type_="check")
        batch.create_check_constraint("ck_classification_decision_source", _NEW)


def downgrade() -> None:
    with op.batch_alter_table("classification_decisions") as batch:
        batch.drop_constraint("ck_classification_decision_source", type_="check")
        batch.create_check_constraint("ck_classification_decision_source", _OLD)
