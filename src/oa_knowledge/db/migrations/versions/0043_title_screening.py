"""Local, versioned title screening and manual audit history."""
from alembic import op
from oa_knowledge.db.models import Base

revision = "0043_title_screening"
down_revision = "0042_manifest_time_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("title_screening_plans", "title_screening_decisions", "title_screening_overrides"):
        Base.metadata.tables[name].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for name in ("title_screening_overrides", "title_screening_decisions", "title_screening_plans"):
        Base.metadata.tables[name].drop(op.get_bind(), checkfirst=True)
