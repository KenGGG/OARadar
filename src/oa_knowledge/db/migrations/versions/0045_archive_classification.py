"""Independent five-category archive classification and local provenance."""
from alembic import op
from oa_knowledge.db.models import Base

revision = "0045_archive_classification"
down_revision = "0044_title_review_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("archive_classification_plans", "archive_classification_results"):
        Base.metadata.tables[name].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for name in ("archive_classification_results", "archive_classification_plans"):
        Base.metadata.tables[name].drop(op.get_bind(), checkfirst=True)
