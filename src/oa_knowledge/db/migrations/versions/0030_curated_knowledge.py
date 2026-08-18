"""Add versioned local curated-knowledge decisions."""

from alembic import op
import sqlalchemy as sa

revision = "0030_curated_knowledge"
down_revision = "0029_oa_gone"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "curated_runs" not in tables:
        op.create_table(
            "curated_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("logical_item_id", sa.Integer(), sa.ForeignKey("logical_items.id", ondelete="CASCADE"), nullable=False),
            sa.Column("input_signature", sa.String(64), nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("rules_version", sa.String(40), nullable=False),
            sa.Column("prompt_version", sa.String(40), nullable=False),
            sa.Column("schema_version", sa.String(40), nullable=False),
            sa.Column("model_name", sa.String(120), nullable=False),
            sa.Column("config_signature", sa.String(64), nullable=False),
            sa.Column("error_code", sa.String(80)),
            sa.Column("error_detail", sa.Text()),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("logical_item_id", "input_signature", name="uq_curated_run_input"),
        )
    tables = _tables()
    if "curated_decisions" not in tables:
        op.create_table(
            "curated_decisions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("curated_run_id", sa.Integer(), sa.ForeignKey("curated_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("knowledge_document_id", sa.Integer(), sa.ForeignKey("knowledge_documents.id", ondelete="SET NULL")),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("document_kind", sa.String(30), nullable=False),
            sa.Column("canonical_key", sa.String(160)),
            sa.Column("normalized_title", sa.Text(), nullable=False),
            sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("decision_hash", sa.String(64), nullable=False),
            sa.Column("output_relpath", sa.Text()),
            sa.Column("review_reason", sa.String(120)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("curated_run_id", "ordinal", name="uq_curated_decision_ordinal"),
            sa.UniqueConstraint("curated_run_id", "decision_hash", name="uq_curated_decision_hash"),
        )
    tables = _tables()
    if "curated_decision_sources" not in tables:
        op.create_table(
            "curated_decision_sources",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("curated_decision_id", sa.Integer(), sa.ForeignKey("curated_decisions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_file_id", sa.Integer(), sa.ForeignKey("files.id", ondelete="SET NULL")),
            sa.Column("source_attachment_id", sa.Integer(), sa.ForeignKey("source_attachments.id", ondelete="SET NULL")),
            sa.Column("archive_member_id", sa.Integer(), sa.ForeignKey("archive_members.id", ondelete="SET NULL")),
            sa.Column("parse_artifact_id", sa.Integer(), sa.ForeignKey("parse_artifacts.id", ondelete="SET NULL")),
            sa.Column("source_key", sa.String(180), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(30), nullable=False),
            sa.Column("content_sha256", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("curated_decision_id", "source_key", name="uq_curated_decision_source_key"),
            sa.UniqueConstraint("curated_decision_id", "ordinal", name="uq_curated_decision_source_ordinal"),
        )


def downgrade() -> None:
    tables = _tables()
    for table in ("curated_decision_sources", "curated_decisions", "curated_runs"):
        if table in tables:
            op.drop_table(table)
