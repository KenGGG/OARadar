"""Keep read-only body review evidence apart from original downloads."""
from alembic import op
from oa_knowledge.db.models import Base

revision = "0044_title_review_evidence"
down_revision = "0043_title_screening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["title_review_evidence"].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["title_review_evidence"].drop(op.get_bind(), checkfirst=True)
