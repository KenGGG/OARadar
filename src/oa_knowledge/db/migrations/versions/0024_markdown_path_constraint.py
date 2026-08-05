"""Allow dots in names while rejecting traversal path segments."""
from alembic import op

revision = "0024_markdown_path_constraint"
down_revision = "0023_markdown_queue"
branch_labels = None
depends_on = None

def upgrade() -> None:
    with op.batch_alter_table("markdown_exports") as batch:
        batch.drop_constraint("ck_markdown_export_relative", type_="check")
        batch.create_check_constraint("ck_markdown_export_relative", "markdown_relpath NOT LIKE '/%' AND markdown_relpath <> '..' AND markdown_relpath NOT LIKE '../%' AND markdown_relpath NOT LIKE '%/../%' AND markdown_relpath NOT LIKE '%/..'")

def downgrade() -> None:
    with op.batch_alter_table("markdown_exports") as batch:
        batch.drop_constraint("ck_markdown_export_relative", type_="check")
        batch.create_check_constraint("ck_markdown_export_relative", "markdown_relpath NOT LIKE '/%' AND markdown_relpath NOT LIKE '%..%'")
