"""Allow unresolved external classifications to await issuer evidence."""

from alembic import op

revision = "0040_external_review_without_issuer"
down_revision = "0039_classification_run_adopted_decision"
branch_labels = None
depends_on = None

_OLD = "content_origin <> 'external' OR (canonical_issuer IS NOT NULL AND trim(canonical_issuer) <> '')"
_NEW = "classification_status <> 'classified' OR content_origin <> 'external' OR (canonical_issuer IS NOT NULL AND trim(canonical_issuer) <> '')"


def upgrade() -> None:
    with op.batch_alter_table("classification_decisions") as batch:
        batch.drop_constraint("ck_classification_external_issuer_required", type_="check")
        batch.create_check_constraint("ck_classification_external_issuer_required", _NEW)


def downgrade() -> None:
    with op.batch_alter_table("classification_decisions") as batch:
        batch.drop_constraint("ck_classification_external_issuer_required", type_="check")
        batch.create_check_constraint("ck_classification_external_issuer_required", _OLD)
